#!/usr/bin/env python3
"""
Blackboard Bot — автоматическое выполнение заданий
Мониторинг курсов → уведомления → AI-решение → отправка
"""

import asyncio
import json
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from dotenv import load_dotenv

from scraper import BlackboardSession
from solver import answer_batch

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MIN_SCORE = int(os.getenv("MIN_SCORE", "85"))
NOTIFY_CHAT_ID = None  # Set on first /start

# State
bb_session: BlackboardSession | None = None
known_assignments: dict[str, dict] = {}  # course_name -> {assignment_name -> data}
pending_assignments: list[dict] = []
active_test: dict | None = None


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    global NOTIFY_CHAT_ID
    NOTIFY_CHAT_ID = message.chat.id
    await message.answer(
        "📚 <b>Blackboard Bot</b>\n\n"
        "Автоматическое выполнение заданий.\n\n"
        "<b>Команды:</b>\n"
        "/check — проверить новые задания\n"
        "/courses — список курсов\n"
        "/status — текущий статус\n"
        "/score — минимальный балл\n\n"
        "Бот сам проверяет курсы каждые 30 мин.",
        parse_mode="HTML",
    )


@router.message(Command("check"))
async def cmd_check(message: Message):
    await message.answer("🔍 Проверяю курсы на новые задания...")
    try:
        if not bb_session:
            await message.answer("⏳ Подключаюсь к Blackboard...")
            await init_session()

        courses = await bb_session.get_courses()
        await message.answer(f"📚 Найдено курсов: {len(courses)}")

        new_assignments = []
        for i, course in enumerate(courses):
            assignments = await bb_session.get_course_assignments(i)
            for a in assignments:
                key = f"{course['name']}::{a['name']}"
                if key not in known_assignments:
                    new_assignments.append({
                        'course': course['name'],
                        'assignment': a['name'],
                        'url': a.get('url', ''),
                        'key': key,
                    })
                    known_assignments[key] = a

        if new_assignments:
            pending_assignments.extend(new_assignments)
            text = "📋 <b>Новые задания:</b>\n\n"
            for a in new_assignments:
                text += f"📖 <b>{a['course']}</b>\n"
                text += f"📝 {a['assignment']}\n\n"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выполнить все", callback_data="do_all")],
                [InlineKeyboardButton(text="👀 Показать детали", callback_data="show_details")],
            ])
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer("✅ Новых заданий нет.")

    except Exception as e:
        logger.error(f"Check error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("courses"))
async def cmd_courses(message: Message):
    try:
        if not bb_session:
            await init_session()
        courses = await bb_session.get_courses()
        text = "📚 <b>Курсы:</b>\n\n"
        for i, c in enumerate(courses, 1):
            text += f"{i}. {c['name']}\n"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("score"))
async def cmd_score(message: Message):
    global MIN_SCORE
    args = message.text.split()
    if len(args) > 1:
        try:
            MIN_SCORE = int(args[1])
            await message.answer(f"✅ Минимальный балл: {MIN_SCORE}%")
        except ValueError:
            await message.answer("Использование: /score 85")
    else:
        await message.answer(f"Текущий минимальный балл: <b>{MIN_SCORE}%</b>\n\nИзменить: /score 90", parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message):
    text = (
        f"<b>📊 Статус</b>\n\n"
        f"Blackboard: {'✅ Подключен' if bb_session and bb_session.logged_in else '❌ Не подключен'}\n"
        f"Известных заданий: {len(known_assignments)}\n"
        f"Ожидающих: {len(pending_assignments)}\n"
        f"Активный тест: {'Да' if active_test else 'Нет'}\n"
        f"Мин. балл: {MIN_SCORE}%"
    )
    await message.answer(text, parse_mode="HTML")


async def init_session():
    """Initialize Blackboard session."""
    global bb_session
    bb_session = BlackboardSession()
    await bb_session.start()


# === Callbacks ===

@router.callback_query(F.data == "do_all")
async def cb_do_all(callback: CallbackQuery):
    if not pending_assignments:
        await callback.answer("Нет заданий для выполнения")
        return

    await callback.answer("Начинаю выполнение...")
    assignment = pending_assignments.pop(0)

    await callback.message.answer(
        f"🔄 <b>Выполняю:</b>\n\n"
        f"📖 {assignment['course']}\n"
        f"📝 {assignment['assignment']}\n\n"
        f"Это может занять несколько минут...",
        parse_mode="HTML",
    )

    try:
        # Navigate to assignment
        result = await bb_session.start_assignment(assignment.get('url', ''))
        questions = result.get('questions', [])

        if not questions:
            await callback.message.answer("❌ Не удалось найти вопросы. Возможно, это задание для загрузки файла.")
            return

        await callback.message.answer(f"📝 Найдено вопросов: {len(questions)}")

        # Answer all questions
        answers = await answer_batch(questions)
        await callback.message.answer(f"🤖 AI ответил на {len(answers)} вопросов. Отправляю...")

        # Submit answers
        for i, answer in enumerate(answers):
            if isinstance(answer, int):
                await bb_session.answer_question(i, answer)

        # Submit test
        result = await bb_session.submit_test()
        score = result.get('percent', 0)

        if score >= MIN_SCORE:
            await callback.message.answer(
                f"✅ <b>Тест сдан!</b>\n\n"
                f"📊 Результат: {score:.0f}%\n"
                f"📝 {assignment['assignment']}",
                parse_mode="HTML",
            )
        else:
            # Retry
            await callback.message.answer(
                f"⚠️ <b>Набрано {score:.0f}%</b> (нужно {MIN_SCORE}%)\n\n"
                f"Пересдаю...",
                parse_mode="HTML",
            )
            # Recursive retry (max 3 times)
            for attempt in range(3):
                await callback.message.answer(f"🔄 Попытка {attempt + 2}/4...")
                result = await bb_session.start_assignment(assignment.get('url', ''))
                questions = result.get('questions', [])
                if not questions:
                    break
                answers = await answer_batch(questions)
                for i, answer in enumerate(answers):
                    if isinstance(answer, int):
                        await bb_session.answer_question(i, answer)
                result = await bb_session.submit_test()
                score = result.get('percent', 0)
                if score >= MIN_SCORE:
                    await callback.message.answer(
                        f"✅ <b>Тест сдан!</b>\n\n📊 Результат: {score:.0f}%",
                        parse_mode="HTML",
                    )
                    break
            else:
                await callback.message.answer(
                    f"❌ Не удалось набрать {MIN_SCORE}% за 4 попытки.\n"
                    f"Последний результат: {score:.0f}%"
                )

    except Exception as e:
        logger.error(f"Do all error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "show_details")
async def cb_show_details(callback: CallbackQuery):
    if not pending_assignments:
        await callback.answer("Нет заданий")
        return

    text = "📋 <b>Детали заданий:</b>\n\n"
    for i, a in enumerate(pending_assignments, 1):
        text += f"{i}. <b>{a['course']}</b>\n   📝 {a['assignment']}\n\n"

    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")


async def auto_check_loop():
    """Background loop to check for new assignments."""
    while True:
        try:
            if bb_session and bb_session.logged_in and NOTIFY_CHAT_ID:
                courses = await bb_session.get_courses()
                new_count = 0
                for i, course in enumerate(courses):
                    assignments = await bb_session.get_course_assignments(i)
                    for a in assignments:
                        key = f"{course['name']}::{a['name']}"
                        if key not in known_assignments:
                            pending_assignments.append({
                                'course': course['name'],
                                'assignment': a['name'],
                                'url': a.get('url', ''),
                                'key': key,
                            })
                            known_assignments[key] = a
                            new_count += 1

                if new_count and NOTIFY_CHAT_ID:
                    bot = Bot(token=BOT_TOKEN)
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Выполнить все", callback_data="do_all")],
                    ])
                    await bot.send_message(
                        NOTIFY_CHAT_ID,
                        f"🔔 <b>Найдено {new_count} новых заданий!</b>\n\nНажми /check для просмотра.",
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    await bot.session.close()

        except Exception as e:
            logger.error(f"Auto check error: {e}")

        await asyncio.sleep(1800)  # 30 minutes


async def main():
    if not BOT_TOKEN:
        print("❌ Missing BOT_TOKEN")
        return

    # Init Blackboard
    try:
        await init_session()
        print("✅ Blackboard connected")
    except Exception as e:
        print(f"⚠️ Blackboard connection failed: {e}")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Start auto-check in background
    asyncio.create_task(auto_check_loop())

    print("📚 Blackboard Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
