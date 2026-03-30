"""Blackboard scraper — login, find assignments, take tests."""
import asyncio
import json
import logging
import os
import re

from playwright.async_api import async_playwright, Page, Browser
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BB_URL = os.getenv("BLACKBOARD_URL", "https://elearn.mu-varna.bg")
BB_USER = os.getenv("BLACKBOARD_USER")
BB_PASS = os.getenv("BLACKBOARD_PASS")


class BlackboardSession:
    """Manages a Blackboard browser session."""

    def __init__(self):
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.logged_in = False

    async def start(self):
        """Launch browser and login."""
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self._login()

    async def close(self):
        if self.browser:
            await self.browser.close()

    async def _login(self, retry=0):
        """Login to Blackboard with retry."""
        if retry > 2:
            raise Exception("Login failed after 3 attempts")
        try:
            await self.page.goto(BB_URL, timeout=60000, wait_until="domcontentloaded")

            # Close privacy dialog
            try:
                ok_btn = self.page.locator('text="OK"').first
                if await ok_btn.is_visible(timeout=5000):
                    await ok_btn.click(force=True)
                    await self.page.wait_for_timeout(1000)
            except Exception:
                pass

            # Fill login form
            await self.page.fill('input#user_id', BB_USER, timeout=10000)
            await self.page.fill('input#password', BB_PASS, timeout=10000)
            await self.page.click('input[type="submit"]')

            # Wait for redirect
            try:
                await self.page.wait_for_url('**/ultra/**', timeout=30000)
            except Exception:
                # Maybe already redirected or different URL pattern
                await self.page.wait_for_timeout(5000)
            await self.page.wait_for_timeout(3000)

            self.logged_in = True
            logger.info(f"Logged in to Blackboard as {BB_USER}")
        except Exception as e:
            logger.warning(f"Login attempt {retry+1} failed: {e}")
            await self.page.wait_for_timeout(5000)
            await self._login(retry + 1)

    async def get_courses(self) -> list[dict]:
        """Get list of all courses."""
        if not self.logged_in:
            await self._login()

        await self.page.goto(f'{BB_URL}/ultra/course', timeout=60000, wait_until='domcontentloaded')
        await self.page.wait_for_timeout(5000)

        courses = []
        # Try to find course elements by various selectors
        selectors = [
            '[data-course-id]',
            '.course-card',
            'li[class*="course"]',
            'a[href*="/ultra/course/"]',
        ]

        for sel in selectors:
            elements = await self.page.query_selector_all(sel)
            if elements:
                for el in elements:
                    text = await el.inner_text()
                    lines = [l.strip() for l in text.split('\n') if l.strip() and len(l.strip()) > 3]
                    if lines:
                        courses.append({
                            'name': lines[0][:100],
                            'index': len(courses),
                            'selector': sel,
                        })
                break

        # Fallback: parse text
        if not courses:
            text = await self.page.inner_text('body')
            lines = text.split('\n')
            for i, line in enumerate(lines):
                line = line.strip()
                if len(line) > 5 and 'Открыть' not in line and 'ultra' not in line.lower() and not line.startswith('©'):
                    if i + 1 < len(lines) and 'Открыть' in lines[i + 1]:
                        courses.append({
                            'name': line[:100],
                            'index': len(courses),
                        })

        logger.info(f"Found {len(courses)} courses")
        return courses

    async def get_course_assignments(self, course_index: int) -> list[dict]:
        """Navigate to course and find assignments."""
        if not self.logged_in:
            await self._login()

        await self.page.goto(f'{BB_URL}/ultra/course', timeout=30000)
        await self.page.wait_for_timeout(3000)

        # Click on course
        open_buttons = self.page.locator('text="Открыть"')
        count = await open_buttons.count()
        if course_index < count:
            await open_buttons.nth(course_index).click()
            await self.page.wait_for_timeout(3000)

        # Look for assignments section
        assignments = []
        text = await self.page.inner_text('body')

        # Parse assignments from page
        if 'задание' in text.lower() or 'assignment' in text.lower() or 'тест' in text.lower():
            # Find assignment links
            links = await self.page.query_selector_all('a')
            for link in links:
                href = await link.get_attribute('href') or ''
                link_text = await link.inner_text()
                if any(kw in href.lower() or kw in link_text.lower()
                       for kw in ['assign', 'test', 'quiz', 'задан']):
                    assignments.append({
                        'name': link_text.strip()[:100],
                        'url': href,
                    })

        return assignments

    async def start_assignment(self, assignment_url: str) -> dict:
        """Navigate to assignment and get questions."""
        if not assignment_url.startswith('http'):
            assignment_url = f'{BB_URL}{assignment_url}'

        await self.page.goto(assignment_url, timeout=30000)
        await self.page.wait_for_timeout(3000)

        # Look for "Begin" or "Start" button
        for selector in ['text="Begin"', 'text="Start"', 'text="Начать"', 'text="Начнете"',
                         'button:has-text("Begin")', 'input[value*="Begin"]']:
            btn = self.page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await self.page.wait_for_timeout(3000)
                break

        return await self._extract_questions()

    async def _extract_questions(self) -> dict:
        """Extract questions from current test page."""
        questions = []
        text = await self.page.inner_text('body')

        # Find question elements
        q_elements = await self.page.query_selector_all(
            '[class*="question"], [data-attempt], fieldset, [role="group"]'
        )

        for q_el in q_elements:
            q_text = await q_el.inner_text()
            if len(q_text) > 10:
                # Find answer options (radio buttons, checkboxes)
                options = []
                inputs = await q_el.query_selector_all('input[type="radio"], input[type="checkbox"]')
                labels = await q_el.query_selector_all('label')

                for label in labels:
                    label_text = await label.inner_text()
                    if label_text.strip():
                        options.append(label_text.strip())

                if options:
                    questions.append({
                        'text': q_text.strip()[:500],
                        'options': options[:10],
                    })

        # If no structured questions found, try parsing text
        if not questions:
            # Split by numbered patterns
            parts = re.split(r'\n\d+[\.\)]\s', text)
            for part in parts[1:]:  # Skip first (before Q1)
                if len(part) > 20:
                    questions.append({
                        'text': part.strip()[:500],
                        'options': [],
                    })

        return {
            'questions': questions,
            'total': len(questions),
            'page_text': text[:5000],
        }

    async def answer_question(self, question_index: int, answer_index: int) -> bool:
        """Select an answer for a question."""
        try:
            q_elements = await self.page.query_selector_all(
                '[class*="question"], [data-attempt], fieldset, [role="group"]'
            )
            if question_index < len(q_elements):
                q_el = q_elements[question_index]
                inputs = await q_el.query_selector_all('input[type="radio"], input[type="checkbox"]')
                if answer_index < len(inputs):
                    await inputs[answer_index].click()
                    return True
        except Exception as e:
            logger.error(f"Failed to answer Q{question_index}: {e}")
        return False

    async def submit_test(self) -> dict:
        """Submit the test and get results."""
        # Look for submit/save button
        for selector in ['text="Submit"', 'text="Save"', 'text="Отправить"', 'text="Сохранить"',
                         'text="Submit All"', 'button:has-text("Submit")',
                         'input[value*="Submit"]', 'input[value*="Save"]']:
            btn = self.page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await self.page.wait_for_timeout(2000)
                break

        # Confirm submission if dialog appears
        for selector in ['text="OK"', 'text="Yes"', 'text="Да"', 'button:has-text("OK")']:
            btn = self.page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await self.page.wait_for_timeout(3000)
                break

        # Get results
        await self.page.wait_for_timeout(3000)
        text = await self.page.inner_text('body')

        # Parse score
        score_match = re.search(r'(\d+)\s*(?:из|of|/)\s*(\d+)', text)
        percent_match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)

        score = 0
        total = 100
        if score_match:
            score = int(score_match.group(1))
            total = int(score_match.group(2))
        elif percent_match:
            score = float(percent_match.group(1))
            total = 100

        return {
            'score': score,
            'total': total,
            'percent': (score / total * 100) if total > 0 else 0,
            'text': text[:1000],
        }

    async def screenshot(self) -> bytes:
        """Take screenshot of current page."""
        return await self.page.screenshot(type='jpeg', quality=85)


    async def ensure_logged_in(self):
        """Re-login if session expired."""
        try:
            # Try navigating to courses to check if still logged in
            await self.page.goto(f'{BB_URL}/ultra/course', timeout=10000)
            await self.page.wait_for_timeout(2000)
            if 'login' in self.page.url.lower() or 'auth' in self.page.url.lower():
                logger.info("Session expired, re-logging in...")
                self.logged_in = False
                await self._login()
        except Exception as e:
            logger.warning(f"Session check failed: {e}")
            self.logged_in = False
            await self._login()
