"""AI solver — answer questions using LLM."""
import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

API_URL = "http://82.24.110.51:20128/v1/chat/completions"
MODEL = "kr/claude-haiku-4.5"


async def answer_question(question: str, options: list[str] = None, context: str = "") -> str | int:
    """Use AI to answer a question. Returns answer text or option index."""
    
    if options:
        options_text = "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options))
        prompt = f"""Answer this multiple choice question. Return ONLY the number (1, 2, 3, etc.) of the correct answer.

Question: {question}

Options:
{options_text}

{f"Context: {context[:500]}" if context else ""}

Return ONLY the number, nothing else."""
    else:
        prompt = f"""Answer this question accurately and concisely.

Question: {question}

{f"Context: {context[:500]}" if context else ""}

Return a concise, correct answer."""

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 500,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, json=payload, headers={"Content-Type": "application/json"},
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                
                # If multiple choice, extract number
                if options:
                    match = __import__('re').search(r'\d+', answer)
                    if match:
                        idx = int(match.group()) - 1
                        if 0 <= idx < len(options):
                            return idx
                    # Try matching text
                    for i, opt in enumerate(options):
                        if answer.lower() in opt.lower() or opt.lower() in answer.lower():
                            return i
                    return 0  # Default to first option
                
                return answer
    except Exception as e:
        logger.error(f"AI solver error: {e}")
        return ""


async def answer_batch(questions: list[dict]) -> list:
    """Answer multiple questions."""
    answers = []
    for q in questions:
        answer = await answer_question(
            q.get('text', ''),
            q.get('options', []),
            q.get('context', ''),
        )
        answers.append(answer)
        await asyncio.sleep(0.5)  # Rate limiting
    return answers
