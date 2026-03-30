#!/usr/bin/env python3
"""
Blackboard Bot — автоматическое выполнение заданий
Мониторинг курсов → уведомления → AI-решение → отправка
"""

import asyncio
import logging
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from dotenv import load_dotenv

from scraper import BlackboardSession
from solver import answer_batch, generate_text_answer, SolverConfig

# ─── Configuration ───────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
MIN_SCORE: int = int(os.getenv("MIN_SCORE", "85"))
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "30")) * 60


# ─── State ───────────────────────────────────────────────────────

@dataclass
class TestResult:
    """Результат прохождения теста."""
    course: str
    assignment: str
    score: float
    attempts: int
    time: datetime
    status: str  # 'passed' | 'failed' | 'skipped' | 'text_generated'


@dataclass
class AppState:
    """Глобальное состояние бота."""
    notify_chat_id: Optional[int] = None
    bb_session: Optional[BlackboardSession] = None
    bb_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    known_assignments: dict = field(default_factory=dict)
    pending_assignments: list = field(default_factory=list)
    test_history: list = field(default_factory=list)
    is_checking: bool = False
    # Для текстовых заданий: хранит сгенерированный текст для подтверждения
    pending_text_answer: dict = field(default_factory=dict)


state = AppState()
router = Router()


# ─── Helpers ─────────────────────────────────────────────────────

async def ensure_bb_session() -> BlackboardSession:
    """Гарантирует живую сессию Blackboard."""
    async with state.bb_lock:
        if state.bb_session and state.bb_session.logged_in:
            return state.bb_session
        state.bb_session = BlackboardSession()
        await state.bb_session.start()
        return state.bb_session


async def safe_session_exec(coro, retries: int = 2):
    """Выполняет корутину с retry при ошибке сессии."""
    for attempt in range(retries + 1):
        try:
            session = await ensure_bb_session()
            return await coro(session)
        except Exception as e:
            logger.warning(f"Session exec attempt {attempt + 1} failed: {e}")
            if attempt < retries:
                state.bb_session = None
                await asyncio.sleep(2)
            else:
                raise


# ─── Commands ────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    state.notify_chat_id = message.chat.id
    await message.answer(
        "📚 <b>Blackboard Bot</b>\n\n"
        "Автоматическое выполнение заданий.\n\n"
        "<b>Команды:</b>\n"
        "/check — проверить новые задания\n"
        "/courses — список курсов\n"
        "/status — текущий статус\n"
        "/score — минимальный балл\n"
        "/history — история тестов\n\n"
        "Бот сам проверяет курсы каждые 30 мин.",
        parse_mode="HTML",
    )


@router.message(Command("check"))
async def cmd_check(message: Message):
    if state.is_checking:
        await message.answer("⏳ Проверка уже идёт, подожди...")
        return

    state.is_checking = True
    await message.answer("🔍 Проверяю курсы на новые задания...")

    try:
        async def do_check(session: BlackboardSession):
            courses = await session.get_courses()
            await message.answer(f"📚 Найдено курсов: {len(courses)}")

            new_assignments = []
            for i, course in enumerate(courses):
                try:
                    assignments = await session.get_course_assignments(i)
                except Exception as e:
                    logger.error(f"Error getting assignments for course {i}: {e}")
                    continue

                for a in assignments:
                    key = f"{course['name']}::{a['name']}"
                    if key not in state.known_assignments:
                        new_assignments.append({
                            'course': course['name'],
                            'assignment': a['name'],
                            'url': a.get('url', ''),
                            'key': key,
                        })
                        state.known_assignments[key] = a

            return new_assignments

        new_assignments = await safe_session_exec(do_check)

        if new_assignments:
            state.pending_assignments.extend(new_assignments)
            text = "📋 <b>Новые задания:</b>\n\n"
            for a in new_assignments:
                text += f"📖 <b>{a['course']}</b>\n📝 {a['assignment']}\n\n"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выполнить все", callback_data="do_all")],
                [InlineKeyboardButton(text="👀 Детали", callback_data="show_details")],
            ])
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer("✅ Новых заданий нет.")

    except Exception as e:
        logger.error(f"Check error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        state.is_checking = False


@router.message(Command("courses"))
async def cmd_courses(message: Message):
    try:
        async def get_courses(session: BlackboardSession):
            return await session.get_courses()

        courses = await safe_session_exec(get_courses)
        if not courses:
            await message.answer("Курсы не найдены.")
            return

        text = "📚 <b>Курсы:</b>\n\n"
        for i, c in enumerate(courses, 1):
            text += f"{i}. {c['name']}\n"
        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Courses error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("score"))
async def cmd_score(message: Message):
    global MIN_SCORE
    args = message.text.split()
    if len(args) > 1:
        try:
            new_score = int(args[1])
            if not 0 < new_score <= 100:
                await message.answer("Балл должен быть от 1 до 100")
                return
            MIN_SCORE = new_score
            await message.answer(f"✅ Минимальный балл: {MIN_SCORE}%")
        except ValueError:
            await message.answer("Использование: /score 85")
    else:
        await message.answer(
            f"Текущий минимальный балл: <b>{MIN_SCORE}%</b>\n\nИзменить: /score 90",
            parse_mode="HTML"
        )


@router.message(Command("status"))
async def cmd_status(message: Message):
    connected = state.bb_session and state.bb_session.logged_in
    text = (
        f"<b>📊 Статус</b>\n\n"
        f"Blackboard: {'✅ Подключен' if connected else '❌ Не подключен'}\n"
        f"Известных заданий: {len(state.known_assignments)}\n"
        f"Ожидающих: {len(state.pending_assignments)}\n"
        f"Проверка: {'⏳ Идёт' if state.is_checking else '—'}\n"
        f"Мин. балл: {MIN_SCORE}%\n"
        f"История: {len(state.test_history)} тестов"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("history"))
async def cmd_history(message: Message):
    if not state.test_history:
        await message.answer("История пуста.")
        return

    icons = {
        'passed': '✅', 'failed': '❌',
        'skipped': '⏭️', 'text_generated': '📝'
    }

    text = "<b>📋 История:</b>\n\n"
    for i, item in enumerate(reversed(state.test_history[-10:]), 1):
        t = item.time.strftime("%d.%m %H:%M")
        icon = icons.get(item.status, '❓')
        score_text = f"{item.score:.0f}%" if item.status in ('passed', 'failed') else item.status
        text += (
            f"{i}. {icon} {score_text} — {item.assignment[:40]}\n"
            f"   📖 {item.course[:30]} • {item.attempts} поп. • {t}\n\n"
        )
    await message.answer(text, parse_mode="HTML")


# ─── Callbacks ───────────────────────────────────────────────────

@router.callback_query(F.data == "show_details")
async def cb_show_details(callback: CallbackQuery):
    if not state.pending_assignments:
        await callback.answer("Нет заданий")
        return

    text = "📋 <b>Детали заданий:</b>\n\n"
    for i, a in enumerate(state.pending_assignments, 1):
        text += f"{i}. <b>{a['course']}</b>\n   📝 {a['assignment']}\n\n"

    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "do_all")
async def cb_do_all(callback: CallbackQuery):
    if not state.pending_assignments:
        await callback.answer("Нет заданий для выполнения")
        return

    await callback.answer("Начинаю выполнение...")

    while state.pending_assignments:
        assignment = state.pending_assignments.pop(0)
        await _execute_assignment(callback.message, assignment)


@router.callback_query(F.data.startswith("text_submit_"))
async def cb_text_submit(callback: CallbackQuery):
    """Подтверждение отправки текстового ответа."""
    assignment_key = callback.data.replace("text_submit_", "")
    pending = state.pending_text_answer.pop(assignment_key, None)

    if not pending:
        await callback.answer("Ответ устарел или уже отправлен")
        return

    await callback.answer("Отправляю текст...")
    await callback.message.edit_text(
        f"✅ <b>Текст отправлен!</b>\n\n"
        f"📖 {pending['course']}\n"
        f"📝 {pending['assignment']}\n\n"
        f"Ответ скопирован — вставь в Blackboard.",
        parse_mode="HTML"
    )

    state.test_history.append(TestResult(
        course=pending['course'],
        assignment=pending['assignment'],
        score=0,
        attempts=1,
        time=datetime.now(),
        status='text_generated',
    ))


@router.callback_query(F.data.startswith("text_skip_"))
async def cb_text_skip(callback: CallbackQuery):
    """Пропуск текстового задания."""
    assignment_key = callback.data.replace("text_skip_", "")
    state.pending_text_answer.pop(assignment_key, None)

    await callback.answer("Пропущено")
    await callback.message.edit_text("⏭️ Задание пропущено.")


@router.callback_query(F.data == "skip_assignment")
async def cb_skip_assignment(callback: CallbackQuery):
    """Пропуск текущего задания."""
    await callback.answer("Пропускаю...")
    # pending_assignments уже pop-нут при входе в _execute_assignment
    # просто логируем
    if callback.message.text:
        await callback.message.edit_text("⏭️ Задание пропущено.")


# ─── Assignment Execution ────────────────────────────────────────

async def _execute_assignment(message: Message, assignment: dict) -> None:
    """Выполнить одно задание с автоматической пересдачей."""
    course = assignment['course']
    name = assignment['assignment']
    url = assignment.get('url', '')
    assignment_key = f"{course}::{name}"

    await message.answer(
        f"🔄 <b>Выполняю:</b>\n\n"
        f"📖 {course}\n📝 {name}\n\n"
        f"Это может занять несколько минут...",
        parse_mode="HTML",
    )

    try:
        async def start_and_check(session: BlackboardSession):
            result = await session.start_assignment(url)
            questions = result.get('questions', [])
            return questions

        questions = await safe_session_exec(start_and_check)

        if questions:
            # Это тест — выполняем как обычно
            await _execute_test(message, assignment, questions)
        else:
            # Не тест — проверяем тип задания
            await _handle_non_test_assignment(message, assignment)

    except Exception as e:
        logger.error(f"Assignment execution error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


async def _execute_test(message: Message, assignment: dict, questions: list) -> None:
    """Выполнить тест с пересдачей."""
    course = assignment['course']
    name = assignment['assignment']
    url = assignment.get('url', '')
    max_attempts = 4

    for attempt in range(1, max_attempts + 1):
        try:
            async def do_test(session: BlackboardSession):
                if attempt > 1:
                    result = await session.start_assignment(url)
                    q = result.get('questions', [])
                else:
                    q = questions

                if not q:
                    return None, 0, "no_questions"

                await message.answer(f"📝 Попытка {attempt}/{max_attempts}: {len(q)} вопросов")

                answers = await answer_batch(q)

                for i, answer in enumerate(answers):
                    if isinstance(answer, int):
                        await session.answer_question(i, answer)

                result = await session.submit_test()
                return result, len(answers), "ok"

            result, answered, status = await safe_session_exec(do_test)

            if status == "no_questions":
                await message.answer("❌ Не удалось найти вопросы.")
                return

            score = result.get('percent', 0)

            if score >= MIN_SCORE:
                state.test_history.append(TestResult(
                    course=course, assignment=name,
                    score=score, attempts=attempt,
                    time=datetime.now(), status='passed',
                ))
                await message.answer(
                    f"✅ <b>Тест сдан!</b>\n\n"
                    f"📊 Результат: {score:.0f}%\n"
                    f"🔄 Попыток: {attempt}\n"
                    f"📝 {name}",
                    parse_mode="HTML",
                )
                return

            if attempt < max_attempts:
                await message.answer(
                    f"⚠️ <b>Набрано {score:.0f}%</b> (нужно {MIN_SCORE}%)\n"
                    f"Пересдаю (попытка {attempt + 1}/{max_attempts})...",
                    parse_mode="HTML",
                )

        except Exception as e:
            logger.error(f"Test attempt {attempt} error: {e}", exc_info=True)
            if attempt < max_attempts:
                await message.answer(f"⚠️ Ошибка: {e}\nПовторяю...")
                await asyncio.sleep(3)

    # Все попытки исчерпаны
    state.test_history.append(TestResult(
        course=course, assignment=name,
        score=score if 'score' in dir() else 0,
        attempts=max_attempts, time=datetime.now(), status='failed',
    ))
    await message.answer(f"❌ Не удалось набрать {MIN_SCORE}% за {max_attempts} попыток.")


async def _handle_non_test_assignment(message: Message, assignment: dict) -> None:
    """Обработка нетестового задания (эссе, файл и т.д.)."""
    course = assignment['course']
    name = assignment['assignment']
    assignment_key = f"{course}::{name}"

    try:
        async def extract_text(session: BlackboardSession):
            return await session.extract_assignment_text()

        info = await safe_session_exec(extract_text)
        prompt = info.get('prompt', '')
        assign_type = info.get('type', 'unknown')

        if not prompt:
            await message.answer("⏭️ Не удалось определить тип задания. Пропускаю.")
            state.test_history.append(TestResult(
                course=course, assignment=name,
                score=0, attempts=0,
                time=datetime.now(), status='skipped',
            ))
            return

        type_labels = {
            'essay': '📝 Эссе/текст',
            'upload': '📁 Загрузка файла',
            'unknown': '❓ Неизвестный тип',
        }
        type_label = type_labels.get(assign_type, '❓ Задание')

        await message.answer(
            f"{type_label}\n\n"
            f"📖 <b>{course}</b>\n"
            f"📝 {name}\n\n"
            f"<b>Инструкция:</b>\n{prompt[:1000]}...",
            parse_mode="HTML",
        )

        if assign_type == 'upload':
            await message.answer("📁 Это задание на загрузку файла. Пропускаю.")
            state.test_history.append(TestResult(
                course=course, assignment=name,
                score=0, attempts=0,
                time=datetime.now(), status='skipped',
            ))
            return

        # Генерируем текстовый ответ через AI
        await message.answer("🤖 Генерирую ответ через AI...")

        async def gen_text(session: BlackboardSession):
            page_text = info.get('page_text', '')
            return await generate_text_answer(prompt, page_text)

        text_answer = await safe_session_exec(gen_text)

        if not text_answer:
            await message.answer("❌ Не удалось сгенерировать ответ. Пропускаю.")
            state.test_history.append(TestResult(
                course=course, assignment=name,
                score=0, attempts=0,
                time=datetime.now(), status='skipped',
            ))
            return

        # Сохраняем для подтверждения
        state.pending_text_answer[assignment_key] = {
            'course': course,
            'assignment': name,
            'text': text_answer,
        }

        # Показываем ответ с кнопками
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data=f"text_submit_{assignment_key}"),
                InlineKeyboardButton(text="❌ Пропустить", callback_data=f"text_skip_"),
            ],
        ])

        # Обрезаем текст если слишком длинный
        display_text = text_answer
        if len(display_text) > 3500:
            display_text = display_text[:3500] + "\n\n... (обрезано)"

        await message.answer(
            f"📝 <b>Сгенерированный ответ:</b>\n\n{display_text}\n\n"
            f"Отправь или пропусти?",
            parse_mode="HTML",
            reply_markup=kb,
        )

    except Exception as e:
        logger.error(f"Non-test assignment error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")
        state.test_history.append(TestResult(
            course=course, assignment=name,
            score=0, attempts=0,
            time=datetime.now(), status='skipped',
        ))


# ─── Auto-check loop ────────────────────────────────────────────

async def auto_check_loop():
    """Фоновая проверка курсов на новые задания."""
    await asyncio.sleep(60)

    while True:
        try:
            if state.notify_chat_id and not state.is_checking:
                session = await ensure_bb_session()
                courses = await session.get_courses()

                new_count = 0
                for i, course in enumerate(courses):
                    try:
                        assignments = await session.get_course_assignments(i)
                    except Exception:
                        continue

                    for a in assignments:
                        key = f"{course['name']}::{a['name']}"
                        if key not in state.known_assignments:
                            state.pending_assignments.append({
                                'course': course['name'],
                                'assignment': a['name'],
                                'url': a.get('url', ''),
                                'key': key,
                            })
                            state.known_assignments[key] = a
                            new_count += 1

                if new_count:
                    bot = Bot(token=BOT_TOKEN)
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Выполнить все", callback_data="do_all")],
                    ])
                    await bot.send_message(
                        state.notify_chat_id,
                        f"🔔 <b>Найдено {new_count} новых заданий!</b>\n\nНажми /check для просмотра.",
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    await bot.session.close()

        except Exception as e:
            logger.error(f"Auto check error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# ─── Entry point ─────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        logger.error("Missing BOT_TOKEN in .env")
        return

    try:
        await ensure_bb_session()
        logger.info("✅ Blackboard connected")
    except Exception as e:
        logger.warning(f"⚠️ Blackboard connection failed: {e}")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(auto_check_loop())

    logger.info("📚 Blackboard Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
