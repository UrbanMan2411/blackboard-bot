"""AI solver — answer questions using LLM with retry logic."""
import asyncio
import logging
from typing import Optional, List, Dict, Any, Union

import aiohttp

logger = logging.getLogger(__name__)


class SolverConfig:
    """Configuration for the AI solver."""
    API_URL: str = "http://82.24.110.51:20128/v1/chat/completions"
    MODEL: str = "kr/claude-haiku-4.5"
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 1.0
    REQUEST_TIMEOUT: int = 30
    RATE_LIMIT_DELAY: float = 0.5


async def answer_question(
    question: str,
    options: Optional[List[str]] = None,
    context: str = "",
    config: Optional[SolverConfig] = None
) -> Union[str, int]:
    """
    Use AI to answer a question with retry logic.

    Args:
        question: The question text
        options: List of answer options for multiple choice questions
        context: Additional context (e.g., assignment content)
        config: Solver configuration (uses defaults if None)

    Returns:
        For multiple choice: index of selected option (0-based)
        For open-ended: answer text string
        Returns empty string or 0 on failure
    """
    if config is None:
        config = SolverConfig()

    if not question or not question.strip():
        logger.warning("Empty question provided")
        return 0 if options else ""

    for attempt in range(config.MAX_RETRIES):
        try:
            return await _make_api_call(question, options, context, config)
        except aiohttp.ClientError as e:
            logger.warning(f"API request failed (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY * (attempt + 1))
            else:
                logger.error(f"All {config.MAX_RETRIES} attempts failed for question: {question[:50]}...")
        except Exception as e:
            logger.error(f"Unexpected error answering question: {e}", exc_info=True)
            break

    return 0 if options else ""


async def _make_api_call(
    question: str,
    options: Optional[List[str]],
    context: str,
    config: SolverConfig
) -> Union[str, int]:
    """Make a single API call to the LLM."""
    if options:
        options_text = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
        prompt = (
            "Answer this multiple choice question. Return ONLY the number (1, 2, 3, etc.) "
            "of the correct answer.\n\n"
            f"Question: {question}\n\n"
            f"Options:\n{options_text}"
            f"\n\nContext: {context[:500]}" if context else ""
            "\n\nReturn ONLY the number, nothing else."
        )
    else:
        prompt = (
            "Answer this question accurately and concisely.\n\n"
            f"Question: {question}"
            f"\n\nContext: {context[:500]}" if context else ""
        )

    payload = {
        "model": config.MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 500,
    }

    timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            config.API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"API returned {resp.status}: {error_text}")
                raise aiohttp.ClientError(f"HTTP {resp.status}")

            data = await resp.json()
            answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            if not answer:
                logger.warning("Empty response from AI")
                return 0 if options else ""

            # Process multiple choice answer
            if options:
                return _parse_multiple_choice_answer(answer, options)

            return answer


def _parse_multiple_choice_answer(answer: str, options: List[str]) -> int:
    """Parse AI response to extract option index."""
    import re

    # Try to extract a number
    match = re.search(r'\d+', answer)
    if match:
        idx = int(match.group()) - 1
        if 0 <= idx < len(options):
            return idx

    # Try to match answer text with options
    answer_lower = answer.lower()
    for i, opt in enumerate(options):
        opt_lower = opt.lower()
        if answer_lower in opt_lower or opt_lower in answer_lower:
            return i

    # Fallback to first option
    logger.warning(f"Could not parse answer '{answer[:50]}', defaulting to option 1")
    return 0


async def generate_text_answer(
    prompt: str,
    context: str = "",
    config: Optional[SolverConfig] = None
) -> str:
    """
    Генерирует текстовый ответ для эссе/задания на загрузку.

    Args:
        prompt: Текст задания/инструкции
        context: Дополнительный контекст со страницы
        config: Конфигурация

    Returns:
        Сгенерированный текст ответа
    """
    if config is None:
        config = SolverConfig()

    if not prompt or not prompt.strip():
        return ""

    for attempt in range(config.MAX_RETRIES):
        try:
            return await _generate_text_api(prompt, context, config)
        except aiohttp.ClientError as e:
            logger.warning(f"Text generation failed (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY * (attempt + 1))
        except Exception as e:
            logger.error(f"Unexpected error generating text: {e}", exc_info=True)
            break

    return ""


async def _generate_text_api(
    prompt: str,
    context: str,
    config: SolverConfig
) -> str:
    """Make API call to generate text answer."""
    full_prompt = (
        "Напиши ответ на задание. Будь точным, структурированным и информативным. "
        "Пиши на том же языке, что и задание.\n\n"
        f"Задание:\n{prompt}"
    )
    if context:
        full_prompt += f"\n\nДополнительный контекст:\n{context[:1500]}"

    payload = {
        "model": config.MODEL,
        "messages": [{"role": "user", "content": full_prompt}],
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            config.API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"API returned {resp.status}: {error_text}")
                raise aiohttp.ClientError(f"HTTP {resp.status}")

            data = await resp.json()
            answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return answer


async def answer_batch(questions: List[Dict[str, Any]], config: Optional[SolverConfig] = None) -> List[Any]:
    """
    Answer multiple questions with rate limiting.

    Args:
        questions: List of question dictionaries with 'text' and optional 'options'
        config: Solver configuration

    Returns:
        List of answers (indices for multiple choice, strings for open-ended)
    """
    if config is None:
        config = SolverConfig()

    answers = []
    for i, q in enumerate(questions):
        try:
            question_text = q.get('text', '')
            options = q.get('options', [])
            context = q.get('context', '')

            if not question_text:
                logger.warning(f"Question {i + 1} has no text, skipping")
                answers.append(0 if options else "")
                continue

            answer = await answer_question(question_text, options, context, config)
            answers.append(answer)

            # Rate limiting between questions (except after last one)
            if i < len(questions) - 1:
                await asyncio.sleep(config.RATE_LIMIT_DELAY)

        except Exception as e:
            logger.error(f"Error processing question {i + 1}: {e}")
            answers.append(0 if options else "")

    return answers
