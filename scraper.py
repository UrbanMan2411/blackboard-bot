"""Blackboard scraper — login, find assignments, take tests with robust retries."""
import asyncio
import logging
import os
import re
from typing import Optional, List, Dict, Any, Tuple

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Configuration
BB_URL = os.getenv("BLACKBOARD_URL", "https://elearn.mu-varna.bg")
BB_USER = os.getenv("BLACKBOARD_USER")
BB_PASS = os.getenv("BLACKBOARD_PASS")


class ScraperConfig:
    """Configuration for the Blackboard scraper."""
    PAGE_TIMEOUT: int = 60000
    NAVIGATION_TIMEOUT: int = 30000
    WAIT_AFTER_LOAD: int = 3000
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 2.0
    SELECTOR_TIMEOUT: int = 10000
    PRIVACY_DIALOG_TIMEOUT: int = 5000


class BlackboardSession:
    """
    Manages a Blackboard browser session with robust error handling and retries.

    Features:
    - Automatic login with credential persistence
    - Smart cookie/privacy dialog dismissal
    - Reliable course and assignment detection
    - Question extraction with multiple fallback strategies
    - Automatic re-login on session expiry
    """

    def __init__(self, config: Optional[ScraperConfig] = None):
        self.config = config or ScraperConfig()
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.logged_in = False
        self._playwright = None

    async def start(self) -> None:
        """Launch browser and login to Blackboard."""
        if self.logged_in:
            return

        try:
            self._playwright = await async_playwright().start()
            self.browser = await self._playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            self.context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            self.page = await self.context.new_page()

            # Set default timeouts
            self.page.set_default_timeout(self.config.SELECTOR_TIMEOUT)
            self.page.set_default_navigation_timeout(self.config.NAVIGATION_TIMEOUT)

            await self._login_with_retry()
            logger.info("Blackboard session initialized successfully")

        except Exception as e:
            logger.error(f"Failed to start Blackboard session: {e}", exc_info=True)
            await self.close()
            raise

    async def close(self) -> None:
        """Cleanly close browser and Playwright resources."""
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.debug(f"Error during cleanup: {e}")
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self._playwright = None
            self.logged_in = False

    async def _login_with_retry(self) -> None:
        """Login to Blackboard with exponential backoff retry."""
        last_error = None

        for attempt in range(self.config.MAX_RETRIES):
            try:
                await self._login()
                return
            except Exception as e:
                last_error = e
                logger.warning(f"Login attempt {attempt + 1}/{self.config.MAX_RETRIES} failed: {e}")
                if attempt < self.config.MAX_RETRIES - 1:
                    delay = self.config.RETRY_DELAY * (2 ** attempt)
                    await asyncio.sleep(delay)
                else:
                    await self.page.screenshot(path='login_error.png') if self.page else None

        raise Exception(f"Login failed after {self.config.MAX_RETRIES} attempts") from last_error

    async def _login(self) -> None:
        """Perform single login attempt."""
        if not self.page:
            raise RuntimeError("Page not initialized")

        # Navigate to Blackboard
        await self.page.goto(BB_URL, wait_until='domcontentloaded', timeout=self.config.PAGE_TIMEOUT)
        await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

        # Dismiss privacy/cookie dialogs
        await self._dismiss_dialogs()

        # Fill login form
        await self.page.fill('input#user_id', BB_USER, timeout=10000)
        await self.page.fill('input#password', BB_PASS, timeout=10000)
        await self.page.click('input[type="submit"]', timeout=10000)

        # Wait for login to complete
        await self._wait_for_login_success()

        self.logged_in = True
        logger.info(f"Logged in as {BB_USER}")

    async def _dismiss_dialogs(self) -> None:
        """Aggressively dismiss any popup dialogs."""
        if not self.page:
            return

        # Common dialog selectors
        dialog_selectors = [
            'button:has-text("OK")',
            'button:has-text("Accept")',
            'button:has-text("Accept All")',
            'button:has-text("Got it")',
            'button:has-text("I Agree")',
            'button:has-text("Dismiss")',
            'button:has-text("Close")',
            'input[value="OK"]',
            'input[value="Accept"]',
            '.lb-closeBtn',
            '.modal-close',
            '[data-dismiss="modal"]',
            '[aria-label="Close"]',
        ]

        for selector in dialog_selectors:
            try:
                buttons = self.page.locator(selector)
                count = await buttons.count()
                for i in range(count):
                    btn = buttons.nth(i)
                    if await btn.is_visible(timeout=1000):
                        await btn.click(force=True)
                        await self.page.wait_for_timeout(500)
            except Exception:
                continue

        # Try JavaScript-based dismissal
        try:
            await self.page.evaluate("""
                (() => {
                    const dialogs = document.querySelectorAll('[role="dialog"], .lb-wrapper, .modal, .overlay');
                    dialogs.forEach(d => {
                        const closeBtn = d.querySelector('[data-dismiss="modal"], .close, .btn-close, button, input[type="submit"]');
                        if (closeBtn) {
                            closeBtn.click();
                            setTimeout(() => d.remove(), 500);
                        } else {
                            d.remove();
                        }
                    });
                })()
            """)
            await self.page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug(f"JS dialog dismissal error: {e}")

    async def _wait_for_login_success(self) -> None:
        """Wait for login to complete and verify we're logged in."""
        # Wait for either redirect to Ultra or detect login failure
        try:
            await self.page.wait_for_url(
                '**/ultra/**',
                timeout=15000
            )
        except PlaywrightTimeoutError:
            # Check if we're still on login page
            current_url = self.page.url
            if 'login' in current_url.lower() or 'auth' in current_url.lower():
                # Try to extract error message
                error_text = await self._extract_login_error()
                raise Exception(f"Login failed: {error_text or 'Unknown error'}")

        # Additional wait for page to stabilize
        await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

        # Verify we're actually logged in
        if not await self._is_logged_in():
            raise Exception("Login verification failed - still not logged in")

    async def _extract_login_error(self) -> str:
        """Extract login error message from page."""
        try:
            # Common error selectors
            error_selectors = [
                '.error',
                '.alert-error',
                '[role="alert"]',
                'span:has-text("Invalid")',
                'div:has-text("Incorrect")',
            ]
            for selector in error_selectors:
                el = self.page.locator(selector).first
                if await el.is_visible(timeout=2000):
                    return await el.inner_text()
        except Exception:
            pass
        return ""

    async def _is_logged_in(self) -> bool:
        """Check if we're currently logged in."""
        try:
            # Check if we can access course page
            await self.page.goto(f'{BB_URL}/ultra/course', wait_until='domcontentloaded', timeout=15000)
            await self.page.wait_for_timeout(2000)

            # If redirected to login, we're not logged in
            if 'login' in self.page.url.lower():
                return False

            # Check for course content
            text = await self.page.inner_text('body')
            return 'course' in text.lower() or 'dashboard' in text.lower()
        except Exception as e:
            logger.debug(f"Login check failed: {e}")
            return False

    async def ensure_logged_in(self) -> None:
        """Ensure session is still valid, re-login if necessary."""
        try:
            if not self.logged_in:
                await self._login_with_retry()
                return

            # Quick check
            await self.page.goto(f'{BB_URL}/ultra/course', wait_until='domcontentloaded', timeout=10000)
            await self.page.wait_for_timeout(2000)

            if 'login' in self.page.url.lower():
                logger.info("Session expired, re-logging in...")
                self.logged_in = False
                await self._login_with_retry()

        except Exception as e:
            logger.warning(f"Session check failed, attempting re-login: {e}")
            self.logged_in = False
            await self._login_with_retry()

    async def get_courses(self) -> List[Dict[str, Any]]:
        """
        Get list of all available courses.

        Returns:
            List of course dictionaries with 'name' and 'index'
        """
        await self.ensure_logged_in()

        await self.page.goto(f'{BB_URL}/ultra/course', wait_until='domcontentloaded', timeout=self.config.PAGE_TIMEOUT)
        await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

        courses = []

        # Strategy 1: Find course cards by selectors
        selectors = [
            '[data-course-id]',
            '.course-card',
            'li[class*="course"]',
            'a[href*="/ultra/course/"]',
            '.course-list div[class*="card"]',
        ]

        for selector in selectors:
            elements = await self.page.query_selector_all(selector)
            if elements:
                logger.info(f"Found {len(elements)} course elements using selector: {selector}")
                for el in elements[:50]:  # Limit to 50
                    course_name = await self._extract_course_name(el)
                    if course_name:
                        courses.append({
                            'name': course_name[:100],
                            'index': len(courses),
                            'selector': selector,
                        })
                break

        # Strategy 2: Parse from text content
        if not courses:
            logger.info("No courses found with selectors, falling back to text parsing")
            courses = await self._parse_courses_from_text()

        logger.info(f"Extracted {len(courses)} courses")
        return courses

    async def _extract_course_name(self, element) -> Optional[str]:
        """Extract clean course name from a course element."""
        try:
            # Get all text in element and children
            text = await element.inner_text()
            if not text:
                return None

            # Clean and split
            lines = [line.strip() for line in text.split('\n') if line.strip() and len(line.strip()) > 3]

            if lines:
                # Filter out navigation/button text
                exclude_patterns = ['открыть', 'open', 'menu', 'collapse', 'expand', 'ultra', '©']
                for line in lines:
                    line_lower = line.lower()
                    if not any(pattern in line_lower for pattern in exclude_patterns):
                        return line[:200]

        except Exception as e:
            logger.debug(f"Error extracting course name: {e}")

        return None

    async def _parse_courses_from_text(self) -> List[Dict[str, Any]]:
        """Fallback: parse course names from page text."""
        courses = []
        try:
            text = await self.page.inner_text('body')
            lines = text.split('\n')

            for i, line in enumerate(lines):
                line = line.strip()
                if len(line) > 8 and len(line) < 200:
                    # Skip common non-course lines
                    skip_patterns = ['copyright', '©', 'privacy', 'terms', 'all rights', 'version']
                    if any(pattern in line.lower() for pattern in skip_patterns):
                        continue

                    # Check next line for "Open" button
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].lower()
                        if 'открыть' in next_line or 'open' in next_line:
                            courses.append({
                                'name': line[:100],
                                'index': len(courses),
                            })
        except Exception as e:
            logger.error(f"Text parsing failed: {e}")

        return courses

    async def get_course_assignments(self, course_index: int) -> List[Dict[str, Any]]:
        """
        Navigate to a course and find available assignments/tests.

        Args:
            course_index: Index of course in the list (order returned by get_courses)

        Returns:
            List of assignment dictionaries with 'name' and 'url'
        """
        await self.ensure_logged_in()

        try:
            # Navigate to courses page
            await self.page.goto(f'{BB_URL}/ultra/course', wait_until='domcontentloaded', timeout=self.config.NAVIGATION_TIMEOUT)
            await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

            # Find and click "Open" button for the course
            open_buttons = self.page.locator('text="Открыть"')
            count = await open_buttons.count()

            if course_index >= count:
                logger.warning(f"Course index {course_index} out of range (found {count} courses)")
                return []

            await open_buttons.nth(course_index).click()
            await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

            # Wait for course page to load
            await self.page.wait_for_load_state('networkidle', timeout=15000)

            return await self._extract_assignments_from_page()

        except Exception as e:
            logger.error(f"Failed to get assignments for course {course_index}: {e}")
            return []

    async def _extract_assignments_from_page(self) -> List[Dict[str, Any]]:
        """Extract assignments from current course page."""
        assignments = []

        try:
            # Get all links
            links = await self.page.query_selector_all('a')

            for link in links:
                try:
                    href = await link.get_attribute('href') or ''
                    link_text = await link.inner_text()

                    if not link_text or len(link_text.strip()) < 2:
                        continue

                    link_text_lower = link_text.lower()
                    href_lower = href.lower()

                    # Check for assignment/test keywords
                    keywords = ['assign', 'test', 'quiz', 'задан', 'тест', 'prüfung', 'exam', 'lab']
                    if any(keyword in href_lower or keyword in link_text_lower for keyword in keywords):
                        assignments.append({
                            'name': link_text.strip()[:100],
                            'url': href if href.startswith('http') else f'{BB_URL}{href}' if href.startswith('/') else f'{BB_URL}/{href}',
                        })

                except Exception as e:
                    logger.debug(f"Error processing link: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to extract assignments: {e}")

        # Deduplicate by name
        seen = set()
        unique = []
        for a in assignments:
            if a['name'] not in seen:
                seen.add(a['name'])
                unique.append(a)

        return unique

    async def start_assignment(self, assignment_url: str) -> Dict[str, Any]:
        """
        Navigate to assignment and extract questions.

        Args:
            assignment_url: Full URL or relative path to assignment

        Returns:
            Dictionary with 'questions' list and metadata
        """
        await self.ensure_logged_in()

        # Ensure full URL
        if not assignment_url.startswith('http'):
            if assignment_url.startswith('/'):
                assignment_url = f'{BB_URL}{assignment_url}'
            else:
                assignment_url = f'{BB_URL}/{assignment_url}'

        try:
            await self.page.goto(assignment_url, wait_until='domcontentloaded', timeout=self.config.NAVIGATION_TIMEOUT)
            await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

            # Look for and click start button
            await self._click_start_button()

            # Wait for questions to load
            await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)

            return await self._extract_questions()

        except Exception as e:
            logger.error(f"Failed to start assignment: {e}")
            return {'questions': [], 'total': 0, 'error': str(e)}

    async def _click_start_button(self) -> None:
        """Click 'Begin', 'Start', or similar button if present."""
        button_selectors = [
            'text="Begin"',
            'text="Start"',
            'text="Начать"',
            'text="Начнете"',
            'button:has-text("Begin")',
            'button:has-text("Start")',
            'input[value*="Begin"]',
            'input[value*="Start"]',
            'a:has-text("Begin")',
        ]

        for selector in button_selectors:
            try:
                btn = self.page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await self.page.wait_for_timeout(self.config.WAIT_AFTER_LOAD)
                    logger.info(f"Clicked start button with selector: {selector}")
                    return
            except Exception:
                continue

        logger.info("No start button found - assignment may already be started")

    async def _extract_questions(self) -> Dict[str, Any]:
        """
        Extract questions from current test page.

        Uses multiple strategies:
        1. Find question containers and input elements
        2. Parse numbered questions from text content
        3. Extract from JavaScript data if available
        """
        questions = []

        try:
            # Strategy 1: Structured parsing
            questions = await self._extract_structured_questions()

            # Strategy 2: Text-based fallback
            if not questions:
                logger.info("No structured questions found, trying text parsing")
                questions = await self._extract_questions_from_text()

            # Apply context cleanup
            for q in questions:
                q['text'] = self._clean_question_text(q['text'])
                if q.get('options'):
                    q['options'] = [self._clean_option_text(opt) for opt in q['options'] if opt.strip()]

        except Exception as e:
            logger.error(f"Failed to extract questions: {e}")

        return {
            'questions': questions,
            'total': len(questions),
            'page_url': self.page.url,
        }

    async def _extract_structured_questions(self) -> List[Dict[str, Any]]:
        """Extract questions using structured DOM parsing."""
        questions = []

        # Find question containers
        container_selectors = [
            '[class*="question"]',
            '[data-attempt]',
            'fieldset',
            '[role="group"]',
            '.question-container',
            '.item-body',
        ]

        containers = []
        for selector in container_selectors:
            try:
                els = await self.page.query_selector_all(selector)
                if els:
                    containers = els
                    logger.info(f"Found {len(containers)} question containers using {selector}")
                    break
            except Exception:
                continue

        # Process each container
        for container in containers[:50]:  # Reasonable limit
            try:
                q_text = await container.inner_text()
                if not q_text or len(q_text) < 10:
                    continue

                # Extract options
                options = []
                try:
                    # Radio buttons
                    radios = await container.query_selector_all('input[type="radio"]')
                    if radios:
                        for radio in radios[:10]:
                            label = await self._get_label_for_input(radio)
                            if label:
                                options.append(label)

                    # Checkboxes
                    if not options:
                        checkboxes = await container.query_selector_all('input[type="checkbox"]')
                        for cb in checkboxes[:10]:
                            label = await self._get_label_for_input(cb)
                            if label:
                                options.append(label)

                    # Direct labels/li elements
                    if not options:
                        labels = await container.query_selector_all('label')
                        for label in labels[:10]:
                            text = await label.inner_text()
                            if text and len(text.strip()) > 1:
                                options.append(text.strip())

                except Exception as e:
                    logger.debug(f"Error extracting options: {e}")

                if len(q_text) > 10:
                    questions.append({
                        'text': q_text[:500],
                        'options': options[:10],
                    })

            except Exception as e:
                logger.debug(f"Error processing question container: {e}")
                continue

        return questions

    async def _get_label_for_input(self, input_element) -> Optional[str]:
        """Get the label text associated with an input element."""
        try:
            # Try to find associated label via 'for' attribute
            input_id = await input_element.get_attribute('id')
            if input_id:
                label = self.page.locator(f'label[for="{input_id}"]')
                if await label.count() > 0:
                    text = await label.inner_text()
                    if text:
                        return text.strip()

            # Try parent/sibling label
            parent = await input_element.evaluate('el => el.closest("label")')
            if parent:
                text = await parent.inner_text()
                if text:
                    return text.strip()

            # Get nearby text
            sibling = await input_element.evaluate('el => el.nextElementSibling')
            if sibling:
                text = await sibling.inner_text()
                if text:
                    return text.strip()

        except Exception:
            pass

        return None

    async def _extract_questions_from_text(self) -> List[Dict[str, Any]]:
        """Parse questions from plain text content."""
        questions = []

        try:
            text = await self.page.inner_text('body')

            # Split by numbered question patterns
            # Matches: "1.", "1)", "Question 1", etc.
            patterns = [
                r'\n\s*(\d+)[\.\)]\s+',  # "1. " or "1) "
                r'\n\s*Question\s+\d+[\:\.]\s+',  # "Question 1: "
                r'\n\s*Вопрос\s+\d+[\:\.]\s+',  # "Вопрос 1: "
            ]

            combined_pattern = '|'.join(patterns)
            parts = re.split(combined_pattern, text, flags=re.IGNORECASE)

            # Skip intro text before first question
            for i, part in enumerate(parts[1:] if len(parts) > 1 else []):
                part = part.strip()
                if len(part) < 10:
                    continue

                # Try to split options from question text
                question_text = part
                options = []

                # Look for option patterns (a), b), 1., etc.)
                option_lines = []
                lines = part.split('\n')
                for line in lines:
                    line_stripped = line.strip()
                    option_match = re.match(r'^[a-dА-Д]?[\.\)\]]\s+', line_stripped, re.IGNORECASE)
                    if option_match:
                        option_lines.append(line_stripped)
                    elif option_lines and len(line_stripped) < 100:
                        # Continuation of option
                        option_lines[-1] += ' ' + line_stripped

                if option_lines:
                    question_text = lines[0] if lines else part
                    options = option_lines

                questions.append({
                    'text': question_text[:500],
                    'options': [opt[:200] for opt in options[:10]],
                })

        except Exception as e:
            logger.error(f"Text-based question extraction failed: {e}")

        return questions

    def _clean_question_text(self, text: str) -> str:
        """Clean question text."""
        # Remove extra whitespace
        text = re.sub(r'\s+', ' ', text)
        return text.strip()[:500]

    def _clean_option_text(self, text: str) -> str:
        """Clean option text."""
        text = re.sub(r'^[a-dА-Д][\.\)\]]\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()[:200]

    async def answer_question(self, question_index: int, answer_index: int) -> bool:
        """
        Select an answer for a specific question.

        Args:
            question_index: Index of question (0-based)
            answer_index: Index of answer option (0-based)

        Returns:
            True if answer was selected, False otherwise
        """
        try:
            # Find question containers
            containers = await self.page.query_selector_all(
                '[class*="question"], [data-attempt], fieldset, [role="group"]'
            )

            if question_index < len(containers):
                container = containers[question_index]

                # Find input elements
                inputs = await container.query_selector_all('input[type="radio"], input[type="checkbox"]')

                if 0 <= answer_index < len(inputs):
                    await inputs[answer_index].click()
                    logger.debug(f"Selected answer {answer_index} for question {question_index}")
                    return True

                logger.warning(f"Answer index {answer_index} out of range for question {question_index} (found {len(inputs)} inputs)")

        except Exception as e:
            logger.error(f"Failed to answer question {question_index}: {e}")

        return False

    async def submit_test(self) -> Dict[str, Any]:
        """
        Submit the current test and retrieve results.

        Returns:
            Dictionary with 'score', 'percent', and raw 'text'
        """
        try:
            # Click submit button
            submit_selectors = [
                'text="Submit"',
                'text="Save"',
                'text="Отправить"',
                'text="Сохранить"',
                'text="Submit All"',
                'button:has-text("Submit")',
                'button:has-text("完成")',
                'input[value*="Submit"]',
                'input[value*="Save"]',
                '[title="Submit"]',
            ]

            for selector in submit_selectors:
                try:
                    btn = self.page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        logger.info(f"Clicked submit button: {selector}")
                        break
                except Exception:
                    continue

            # Handle confirmation dialog
            await self._handle_submit_confirmation()

            # Wait for results
            await self.page.wait_for_timeout(3000)

            # Extract score from page
            return await self._extract_score_from_page()

        except Exception as e:
            logger.error(f"Failed to submit test: {e}")
            return {'score': 0, 'total': 100, 'percent': 0, 'error': str(e)}

    async def _handle_submit_confirmation(self) -> None:
        """Handle submission confirmation dialog if it appears."""
        confirm_selectors = [
            'text="OK"',
            'text="Yes"',
            'text="Да"',
            'text="Подтвердить"',
            'button:has-text("OK")',
            'button:has-text("Yes")',
            'button:has-text("Да")',
            '.confirm-ok',
        ]

        for selector in confirm_selectors:
            try:
                btn = self.page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await self.page.wait_for_timeout(2000)
                    logger.info("Clicked confirmation button")
                    return
            except Exception:
                continue

    async def _extract_score_from_page(self) -> Dict[str, Any]:
        """Extract score percentage from results page."""
        try:
            text = await self.page.inner_text('body')

            # Pattern: "X out of Y" or "X/Y" or "X%"
            score_match = re.search(r'(\d+)\s*(?:из|of|/)\s*(\d+)', text, re.IGNORECASE)
            percent_match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)

            score = 0
            total = 100
            percent = 0.0

            if score_match:
                score = int(score_match.group(1))
                total = int(score_match.group(2))
                percent = (score / total * 100) if total > 0 else 0
            elif percent_match:
                percent = float(percent_match.group(1))
                score = int(percent * total / 100) if total > 0 else int(percent)

            logger.info(f"Score extracted: {score}/{total} ({percent:.1f}%)")

            return {
                'score': score,
                'total': total,
                'percent': percent,
                'text': text[:2000],
            }

        except Exception as e:
            logger.error(f"Failed to extract score: {e}")
            return {'score': 0, 'total': 100, 'percent': 0, 'text': ''}

    async def screenshot(self) -> bytes:
        """Take a screenshot of the current page."""
        try:
            return await self.page.screenshot(type='jpeg', quality=85, full_page=False)
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return b''

    async def extract_assignment_text(self) -> Dict[str, Any]:
        """
        Извлекает текст задания для нетестовых заданий (эссе, файлы и т.д.)

        Returns:
            dict с ключами: prompt (текст задания), type (essay|upload|unknown), page_text
        """
        try:
            text = await self.page.inner_text('body')

            # Определяем тип задания
            assignment_type = 'unknown'
            text_lower = text.lower()

            if any(kw in text_lower for kw in ['essay', 'эссе', 'сочинен', 'напишите', 'опишите', 'расскажите', 'write', 'compose']):
                assignment_type = 'essay'
            elif any(kw in text_lower for kw in ['upload', 'загрузит', 'прикрепит', 'файл', 'file', 'attach', 'submit file']):
                assignment_type = 'upload'
            elif any(kw in text_lower for kw in ['journal', 'журнал', 'reflection', 'рефлекси']):
                assignment_type = 'essay'
            elif any(kw in text_lower for kw in ['discussion', 'обсуждени', 'дискуссия', 'forum', 'форум']):
                assignment_type = 'essay'

            # Извлекаем инструкцию/текст задания
            prompt = await self._extract_assignment_prompt()

            return {
                'prompt': prompt,
                'type': assignment_type,
                'page_text': text[:3000],
            }

        except Exception as e:
            logger.error(f"Failed to extract assignment text: {e}")
            return {'prompt': '', 'type': 'unknown', 'page_text': ''}

    async def _extract_assignment_prompt(self) -> str:
        """Извлекает текст задания/инструкции со страницы."""
        try:
            # Ищем блоки с инструкциями
            selectors = [
                '[class*="instructions"]',
                '[class*="description"]',
                '[class*="assignment-text"]',
                '[data-testid*="instruction"]',
                '.vtbegenerated',  # Blackboard rich text
                '.detailed-description',
                'article',
                'main',
            ]

            for selector in selectors:
                el = self.page.locator(selector).first
                if await el.is_visible(timeout=2000):
                    text = await el.inner_text()
                    if len(text.strip()) > 20:
                        return text.strip()[:3000]

            # Fallback: берём основной текст страницы
            text = await self.page.inner_text('body')
            lines = [l.strip() for l in text.split('\n') if l.strip() and len(l.strip()) > 15]

            # Пропускаем навигацию в начале
            skip_patterns = ['меню', 'menu', 'навигация', 'navigation', 'курс', 'course', '©', 'copyright']
            filtered = []
            started = False
            for line in lines:
                line_lower = line.lower()
                if not started:
                    if any(kw in line_lower for kw in ['задание', 'assignment', 'инструкци', 'instruction', 'описание', 'description']):
                        started = True
                        continue
                if started:
                    if any(p in line_lower for p in skip_patterns):
                        continue
                    filtered.append(line)

            return '\n'.join(filtered[:50])[:3000]

        except Exception as e:
            logger.error(f"Failed to extract assignment prompt: {e}")
            return ''
