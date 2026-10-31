from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any, Coroutine

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import ConfigError, Settings, load_settings, setup_logging
from database import Database
from import_questions import load_questions, validate_questions

OPTION_LABELS = ("A", "B", "C", "D")
OPTION_PREFIX_RE = re.compile(r"^[A-Da-dА-Яа-я]\s*[\).\-\:]\s*")

logger = logging.getLogger(__name__)
router = Router()

settings: Settings
db: Database
bot: Bot

quiz_locks: dict[int, asyncio.Lock] = {}
cancel_events: dict[int, asyncio.Event] = {}
active_sessions: dict[int, int] = {}
current_questions: dict[int, tuple[int, int]] = {}
background_tasks: set[asyncio.Task] = set()


class AddQuestion(StatesGroup):
    grade = State()
    text = State()
    options = State()
    correct = State()
    explanation = State()
    source = State()
    image = State()


class SetImage(StatesGroup):
    identifier = State()
    photo = State()


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def display_name(user) -> str:
    return (user.full_name or user.first_name or user.username or str(user.id)).strip()


def get_target_chat_id() -> int | None:
    raw = db.get_setting("group_chat_id")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Bazadagi group_chat_id noto‘g‘ri: %r", raw)
    return settings.group_chat_id


def target_for_message(message: Message) -> int | None:
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return message.chat.id
    return get_target_chat_id()


def request_cancel(chat_id: int) -> bool:
    event = cancel_events.get(chat_id)
    if event is None or event.is_set():
        return False
    event.set()
    question = current_questions.get(chat_id)
    if question:
        db.close_question(question[0], question[1])
    return True


def spawn(coroutine: Coroutine) -> None:
    task = asyncio.create_task(coroutine)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


def question_keyboard(session_id: int, question: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, label in enumerate(OPTION_LABELS):
        option = str(question[f"option_{label.lower()}"]).replace("\n", " ").strip()
        if len(option) > 60:
            option = option[:57] + "..."
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label}) {option}",
                    callback_data=f"ans:{session_id}:{question['id']}:{index}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_question(
    question: dict[str, Any], position: int, total: int, grade: int
) -> str:
    options = []
    for label in OPTION_LABELS:
        options.append(f"<b>{label})</b> {esc(question[f'option_{label.lower()}'])}")
    return (
        f"<b>Huquq quizi | {grade}-sinf</b>\n"
        f"<b>Savol {position}/{total}</b>\n\n"
        f"<b>{esc(question['text'])}</b>\n\n"
        + "\n".join(options)
        + f"\n\n<i>Javob uchun vaqt: {settings.question_seconds} soniya. "
        "Har bir savolga faqat bir marta javob berish mumkin.</i>"
    )


def format_reveal(
    question: dict[str, Any], position: int, total: int, answered: int, correct: int
) -> str:
    correct_index = int(question["correct_option"])
    correct_label = OPTION_LABELS[correct_index]
    correct_text = question[f"option_{correct_label.lower()}"]
    percentage = int(round(correct * 100 / answered)) if answered else 0
    return (
        f"<b>Savol {position}/{total} yakunlandi</b>\n\n"
        f"<b>To‘g‘ri javob:</b> {correct_label}) {esc(correct_text)}\n\n"
        f"{esc(question['explanation'])}\n\n"
        f"<i>Manba:</i> {esc(question['source'])}\n"
        f"<i>Javob berganlar:</i> {answered} ta, to‘g‘ri: {correct} ta ({percentage}%)"
    )


async def send_question(
    chat_id: int,
    session_id: int,
    question: dict[str, Any],
    position: int,
    total: int,
    grade: int,
) -> None:
    text = format_question(question, position, total, grade)
    keyboard = question_keyboard(session_id, question)
    image_file_id = question.get("image_file_id")
    message = None
    is_photo = False

    if image_file_id:
        try:
            if len(text) <= 1000:
                message = await bot.send_photo(
                    chat_id,
                    image_file_id,
                    caption=text,
                    reply_markup=keyboard,
                )
                is_photo = True
            else:
                await bot.send_photo(
                    chat_id,
                    image_file_id,
                    caption=f"Huquq quizi | {grade}-sinf. Savol {position}/{total}.",
                )
        except TelegramBadRequest as exc:
            logger.warning("Rasmli savol yuborilmadi (%s). Matn bilan yuboriladi.", exc)

    if message is None:
        message = await bot.send_message(chat_id, text, reply_markup=keyboard)
        is_photo = False

    db.set_question_message(session_id, int(question["id"]), message.message_id, is_photo)


async def run_quiz(chat_id: int, grade: int, source: str = "auto") -> None:
    lock = quiz_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        logger.info("Chat %s uchun quiz allaqachon davom etmoqda.", chat_id)
        try:
            await bot.send_message(chat_id, "Hozir quiz davom etmoqda. Iltimos, kuting.")
        except Exception:
            logger.exception("Bandlik haqidagi xabarni yuborishda xato.")
        return

    async with lock:
        session_id: int | None = None
        status = "finished"
        finished_marked = False
        cancel_event = asyncio.Event()
        cancel_events[chat_id] = cancel_event
        try:
            questions = db.pick_questions(
                grade,
                settings.questions_per_quiz,
                settings.recent_question_days,
            )
            if not questions:
                await bot.send_message(
                    chat_id,
                    f"{grade}-sinf uchun faol savollar topilmadi. "
                    "Administrator savollar bazasini to‘ldirishi kerak.",
                )
                return

            session_id = db.create_quiz_session(
                chat_id=chat_id,
                grade=grade,
                questions=questions,
                source=source,
                quiz_date=settings.today().isoformat(),
            )
            active_sessions[chat_id] = session_id
            logger.info(
                "Quiz boshlandi: chat=%s session=%s grade=%s questions=%s source=%s",
                chat_id,
                session_id,
                grade,
                len(questions),
                source,
            )

            await bot.send_message(
                chat_id,
                f"<b>Huquq quizi boshlandi</b>\n\n"
                f"Sinf: <b>{grade}</b>\n"
                f"Savollar: <b>{len(questions)}</b> ta\n"
                f"Har bir savolga <b>{settings.question_seconds} soniya</b>\n\n"
                "Javobni faqat bir marta tanlash mumkin.",
            )

            for position, question in enumerate(questions, start=1):
                if cancel_event.is_set():
                    status = "cancelled"
                    break

                question_id = int(question["id"])
                db.open_question(session_id, question_id, settings.question_seconds)
                current_questions[chat_id] = (session_id, question_id)
                await send_question(
                    chat_id,
                    session_id,
                    question,
                    position,
                    len(questions),
                    grade,
                )

                try:
                    await asyncio.wait_for(
                        cancel_event.wait(), timeout=settings.question_seconds
                    )
                except TimeoutError:
                    pass

                db.close_question(session_id, question_id)
                current_questions.pop(chat_id, None)

                if cancel_event.is_set():
                    status = "cancelled"
                    break

                answered, correct = db.question_answer_stats(session_id, question_id)
                await bot.send_message(
                    chat_id,
                    format_reveal(question, position, len(questions), answered, correct),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                await asyncio.sleep(2)

            db.finish_session(session_id, status)
            finished_marked = True
            if status == "cancelled":
                await bot.send_message(chat_id, "Quiz bekor qilindi.")
                logger.info("Quiz bekor qilindi: chat=%s session=%s", chat_id, session_id)
            else:
                participants = db.get_participants(session_id)
                await bot.send_message(
                    chat_id,
                    format_results(grade, participants),
                )
                logger.info(
                    "Quiz yakunlandi: chat=%s session=%s participants=%s",
                    chat_id,
                    session_id,
                    len(participants),
                )
        except Exception:
            logger.exception("Quiz davomida xato: chat=%s", chat_id)
            if session_id is not None and not finished_marked:
                try:
                    db.finish_session(session_id, "interrupted")
                except Exception:
                    logger.exception("Session holatini yangilashda xato.")
            try:
                await bot.send_message(
                    chat_id,
                    "Texnik xatolik yuz berdi. Quiz to‘xtatildi. "
                    "Administrator loglarni tekshirsin.",
                )
            except Exception:
                logger.exception("Xato haqidagi xabarni yuborishda xato.")
        finally:
            active_sessions.pop(chat_id, None)
            current_questions.pop(chat_id, None)
            cancel_events.pop(chat_id, None)


def clip_name(value: Any, limit: int = 18) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_rating_table(
    rows: list[dict[str, Any]],
    score_key: str = "score",
    correct_key: str = "correct_count",
    answered_key: str = "answered_count",
) -> str:
    header = f"{'#':<3}{'Ism':<20}{'Ball':>5}{'To‘g‘ri':>9}"
    lines = [header, "-" * len(header)]
    for index, row in enumerate(rows, start=1):
        name = clip_name(row["display_name"])
        correct = f"{int(row[correct_key])}/{int(row[answered_key])}"
        lines.append(f"{index:<3}{name:<20}{int(row[score_key]):>5}{correct:>9}")
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def format_results(grade: int, participants: list[dict[str, Any]]) -> str:
    if not participants:
        return (
            f"<b>Huquq quizi yakunlandi | {grade}-sinf</b>\n\n"
            "Bu quizda hech kim javob bermadi."
        )

    shown = participants[:20]
    text = (
        f"<b>Huquq quizi yakunlandi | {grade}-sinf</b>\n\n"
        f"{format_rating_table(shown)}\n\n"
        f"Qatnashchilar: <b>{len(participants)}</b> ta\n"
        "<i>Har bir to‘g‘ri javob uchun 1 ball beriladi.</i>"
    )
    if len(participants) > len(shown):
        text += f"\n<i>Jadvalda eng yaxshi {len(shown)} ta ko‘rsatildi.</i>"
    return text


async def daily_announcement() -> None:
    chat_id = get_target_chat_id()
    if chat_id is None:
        logger.warning("Guruh sozlanmagan. Kunlik e’lon yuborilmadi.")
        return
    grade = settings.grade_for_date(settings.today())
    try:
        await bot.send_message(
            chat_id,
            f"<b>Huquq quizi</b>\n\n"
            f"Bugungi sinf: <b>{grade}</b>\n"
            f"Boshlanish: <b>{settings.quiz_hour:02d}:{settings.quiz_minute:02d}</b>\n"
            f"Savollar: <b>{settings.questions_per_quiz}</b> ta\n"
            f"Har bir savolga <b>{settings.question_seconds} soniya</b>\n\n"
            "Tayyor turing. Javoblar faqat quiz boshlangandan keyin qabul qilinadi.",
        )
        logger.info("Kunlik e’lon yuborildi: chat=%s grade=%s", chat_id, grade)
    except Exception:
        logger.exception("Kunlik e’lonni yuborishda xato: chat=%s", chat_id)


async def daily_quiz() -> None:
    chat_id = get_target_chat_id()
    if chat_id is None:
        logger.warning("Guruh sozlanmagan. Kunlik quiz boshlanmadi.")
        return
    grade = settings.grade_for_date(settings.today())
    await run_quiz(chat_id, grade, "auto")


async def set_commands(bot_instance: Bot) -> None:
    public_commands = [
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="stats", description="Shaxsiy natijalar"),
        BotCommand(command="top", description="Umumiy reyting"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="quiznow", description="Quizni hoziroq boshlash"),
        BotCommand(command="cancelquiz", description="Faol quizni bekor qilish"),
        BotCommand(command="setgroup", description="Quiz guruhini sozlash"),
        BotCommand(command="addquestion", description="Yangi savol qo‘shish"),
        BotCommand(command="setimage", description="Savolga rasm biriktirish"),
        BotCommand(command="reload", description="Savollarni JSONdan qayta yuklash"),
        BotCommand(command="fileid", description="Rasm file_id ni olish"),
    ]

    try:
        await bot_instance.set_my_commands(public_commands)
        await bot_instance.set_my_commands(
            public_commands, scope=BotCommandScopeAllGroupChats()
        )
    except Exception:
        logger.exception("Umumiy buyruqlar menyusini o‘rnatishda xato.")

    for admin_id in settings.admin_ids:
        try:
            await bot_instance.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            logger.warning("Admin %s uchun buyruqlar menyusi o‘rnatilmadi.", admin_id)


async def add_to_group_keyboard() -> InlineKeyboardMarkup | None:
    try:
        me = await bot.me()
    except Exception:
        logger.exception("Bot username olinmadi.")
        return None
    if not me.username:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Guruhga qo‘shish",
                    url=f"https://t.me/{me.username}?startgroup=true",
                )
            ]
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = esc(display_name(message.from_user))
    group_id = get_target_chat_id()
    lines = [
        f"Salom, <b>{name}</b>.",
        "",
        "Bu bot huquq fanidan guruh quizlarini o‘tkazadi.",
        f"Har kuni <b>{settings.quiz_hour:02d}:{settings.quiz_minute:02d}</b> da "
        f"<b>{settings.questions_per_quiz}</b> ta savol bilan quiz boshlanadi.",
        f"Har bir savolga <b>{settings.question_seconds} soniya</b> beriladi.",
    ]
    if is_admin(message.from_user.id):
        if group_id is None:
            lines.append("")
            lines.append("Guruh hali sozlanmagan. Guruhda /setgroup buyrug‘ini yuboring.")
    keyboard = None
    if message.chat.type == ChatType.PRIVATE:
        keyboard = await add_to_group_keyboard()
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    row = db.user_stats(message.from_user.id)
    if not row:
        await message.answer(
            "Sizda hali quiz natijalari yo‘q. Kunlik quizda qatnashing."
        )
        return
    answered = int(row["answered_questions"])
    correct = int(row["correct_answers"])
    percentage = int(round(correct * 100 / answered)) if answered else 0
    text = (
        f"<b>Shaxsiy natijalar</b>\n\n"
        f"Umumiy ball: <b>{row['total_score']}</b>\n"
        f"To‘g‘ri javoblar: <b>{correct}/{answered}</b> ({percentage}%)\n"
        f"Qatnashgan quizlar: <b>{row['quizzes_played']}</b>"
    )
    await message.answer(text)


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    rows = db.top_users(20)
    if not rows:
        await message.answer("Reyting hali shakllanmagan.")
        return
    table = format_rating_table(
        rows,
        score_key="total_score",
        correct_key="correct_answers",
        answered_key="answered_questions",
    )
    await message.answer("<b>Umumiy reyting</b>\n\n" + table)


@router.message(Command("setgroup"))
async def cmd_setgroup(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return

    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        chat_id = message.chat.id
    else:
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Guruhda /setgroup buyrug‘ini yuboring yoki "
                "/setgroup -100... ko‘rinishida chat ID kiriting."
            )
            return
        try:
            chat_id = int(raw)
        except ValueError:
            await message.answer("Chat ID butun son bo‘lishi kerak.")
            return

    if chat_id >= 0:
        await message.answer("Guruh chat ID manfiy son bo‘lishi kerak.")
        return

    db.set_setting("group_chat_id", str(chat_id))
    logger.info("Quiz guruhi sozlandi: chat=%s admin=%s", chat_id, message.from_user.id)
    await message.answer("Quiz guruhi saqlandi.")

    if message.chat.id != chat_id:
        try:
            await bot.send_message(
                chat_id,
                "Bu guruh huquq quizi uchun sozlandi. "
                f"Quiz har kuni {settings.quiz_hour:02d}:{settings.quiz_minute:02d} da boshlanadi.",
            )
        except Exception:
            logger.exception("Sozlangan guruhga sinov xabari yuborilmadi: %s", chat_id)
            await message.answer(
                "Diqqat: bot bu guruhga xabar yubora olmadi. "
                "Bot guruhga qo‘shilganini va administrator ekanini tekshiring."
            )


@router.message(Command("quiznow"))
async def cmd_quiznow(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return

    raw = (command.args or "").strip()
    if raw:
        try:
            grade = int(raw)
        except ValueError:
            await message.answer("Sinf butun son bo‘lishi kerak, masalan: /quiznow 9")
            return
        if not 1 <= grade <= 11:
            await message.answer("Sinf 1 dan 11 gacha bo‘lishi kerak.")
            return
    else:
        grade = settings.grade_for_date(settings.today())

    if message.chat.type == ChatType.PRIVATE:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="O‘zingizga",
                        callback_data=f"qtarget:self:{grade}",
                    ),
                    InlineKeyboardButton(
                        text="Guruhga",
                        callback_data=f"qtarget:group:{grade}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Bekor qilish",
                        callback_data="qtarget:cancel:0",
                    )
                ],
            ]
        )
        await message.answer(
            f"{grade}-sinf quizi qayerga yuborilsin?",
            reply_markup=keyboard,
        )
        return

    chat_id = target_for_message(message)
    if chat_id is None:
        await message.answer(
            "Quiz guruhi sozlanmagan. Avval guruhda /setgroup buyrug‘ini yuboring."
        )
        return

    lock = quiz_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await message.answer("Bu chatda quiz allaqachon davom etmoqda.")
        return

    spawn(run_quiz(chat_id, grade, "manual"))


@router.callback_query(F.data.startswith("qtarget:"))
async def handle_quiz_target(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Bu amal faqat administrator uchun.", show_alert=True)
        return

    try:
        _, target, raw_grade = (callback.data or "").split(":")
        grade = int(raw_grade)
    except ValueError:
        await callback.answer("Noto‘g‘ri ma’lumot.", show_alert=True)
        return

    if target == "cancel":
        if callback.message:
            await callback.message.edit_text("Quiz bekor qilindi.")
        await callback.answer()
        return

    if target == "self":
        chat_id = callback.message.chat.id
        place = "shaxsiy chatda"
    elif target == "group":
        chat_id = get_target_chat_id()
        place = "guruhda"
        if chat_id is None:
            await callback.answer(
                "Quiz guruhi sozlanmagan. Avval /setgroup buyrug‘ini yuboring.",
                show_alert=True,
            )
            return
    else:
        await callback.answer("Noto‘g‘ri ma’lumot.", show_alert=True)
        return

    lock = quiz_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await callback.answer("Bu chatda quiz allaqachon davom etmoqda.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(f"{grade}-sinf quizi {place} boshlandi.")
    spawn(run_quiz(chat_id, grade, "manual"))
    await callback.answer()


@router.message(Command("cancelquiz"))
async def cmd_cancelquiz(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return
    chat_id = target_for_message(message)
    if message.chat.type == ChatType.PRIVATE and message.chat.id in active_sessions:
        chat_id = message.chat.id
    if chat_id is None:
        await message.answer("Quiz guruhi sozlanmagan.")
        return
    if not request_cancel(chat_id):
        await message.answer("Faol quiz topilmadi.")
        return
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("Bekor qilish yuborildi.")


@router.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return
    try:
        items = load_questions(settings.questions_file)
        validate_questions(items)
        added, updated = db.seed_questions(items)
    except Exception as exc:
        logger.exception("Savollarni qayta yuklashda xato.")
        await message.answer(f"Import xatosi: <code>{esc(exc)}</code>")
        return
    await message.answer(
        f"Savollar yuklandi. Qo‘shildi: <b>{added}</b>, yangilandi: <b>{updated}</b>."
    )


@router.message(Command("fileid"))
async def cmd_fileid(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return

    target = message.reply_to_message or message
    file_id = None
    if target.photo:
        file_id = target.photo[-1].file_id
    elif target.document and (target.document.mime_type or "").startswith("image/"):
        file_id = target.document.file_id

    if not file_id:
        await message.answer(
            "Rasmga reply qilib /fileid yuboring."
        )
        return
    await message.answer(f"<code>{esc(file_id)}</code>")


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel_state(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Bekor qilish uchun faol amal yo‘q.")
        return
    await state.clear()
    await message.answer("Amal bekor qilindi.")


@router.message(Command("addquestion"), F.chat.type == ChatType.PRIVATE)
async def add_question_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return
    await state.clear()
    await state.set_state(AddQuestion.grade)
    await message.answer(
        "Yangi savol qo‘shish.\n\n"
        "1-qadam: sinfni kiriting (1–11), masalan: <code>9</code>.\n"
        "Bekor qilish: /cancel"
    )


@router.message(AddQuestion.grade)
async def add_question_grade(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        grade = int((message.text or "").strip())
    except ValueError:
        await message.answer("Sinfni butun son bilan kiriting (1–11).")
        return
    if not 1 <= grade <= 11:
        await message.answer("Sinf 1 dan 11 gacha bo‘lishi kerak.")
        return
    await state.update_data(grade=grade)
    await state.set_state(AddQuestion.text)
    await message.answer("2-qadam: savol matnini kiriting (600 belgidan oshmasin).")


@router.message(AddQuestion.text)
async def add_question_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 5:
        await message.answer("Savol matni juda qisqa.")
        return
    if len(text) > 600:
        await message.answer("Savol matni 600 belgidan oshmasligi kerak.")
        return
    await state.update_data(text=text)
    await state.set_state(AddQuestion.options)
    await message.answer(
        "3-qadam: 4 ta variantni har biri yangi qatorda yuboring.\n"
        "Masalan:\n"
        "<code>A) Birinchi variant\n"
        "B) Ikkinchi variant\n"
        "C) Uchinchi variant\n"
        "D) To‘rtinchi variant</code>"
    )


@router.message(AddQuestion.options)
async def add_question_options(message: Message, state: FSMContext) -> None:
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if len(lines) != 4:
        await message.answer("Aynan 4 ta variant kerak. Har biri yangi qatorda bo‘lsin.")
        return
    options = [OPTION_PREFIX_RE.sub("", line).strip() for line in lines]
    if any(not option for option in options):
        await message.answer("Variantlardan biri bo‘sh qoldi.")
        return
    if any(len(option) > 80 for option in options):
        await message.answer("Har bir variant 80 belgidan oshmasligi kerak.")
        return
    if len({option.casefold() for option in options}) != 4:
        await message.answer("Variantlar bir-biridan farq qilishi kerak.")
        return
    await state.update_data(options=options)
    await state.set_state(AddQuestion.correct)
    await message.answer("4-qadam: to‘g‘ri javobni kiriting: A, B, C yoki D.")


@router.message(AddQuestion.correct)
async def add_question_correct(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().upper()
    mapping = {"A": 0, "B": 1, "C": 2, "D": 3, "1": 0, "2": 1, "3": 2, "4": 3}
    if raw not in mapping:
        await message.answer("To‘g‘ri javobni A, B, C, D yoki 1, 2, 3, 4 bilan kiriting.")
        return
    await state.update_data(correct_option=mapping[raw])
    await state.set_state(AddQuestion.explanation)
    await message.answer(
        "5-qadam: qisqa izoh kiriting. Nima uchun bu javob to‘g‘ri?"
    )


@router.message(AddQuestion.explanation)
async def add_question_explanation(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 5:
        await message.answer("Izoh juda qisqa.")
        return
    if len(text) > 700:
        await message.answer("Izoh 700 belgidan oshmasligi kerak.")
        return
    await state.update_data(explanation=text)
    await state.set_state(AddQuestion.source)
    await message.answer(
        "6-qadam: manbani kiriting. Masalan: "
        "<code>O‘zbekiston Respublikasi Konstitutsiyasi, 19-modda</code>"
    )


@router.message(AddQuestion.source)
async def add_question_source(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Manba juda qisqa.")
        return
    if len(text) > 300:
        await message.answer("Manba 300 belgidan oshmasligi kerak.")
        return
    await state.update_data(source=text)
    await state.set_state(AddQuestion.image)
    await message.answer(
        "7-qadam: savol uchun rasm yuboring yoki /skip yuboring."
    )


@router.message(AddQuestion.image)
async def add_question_image(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    image_file_id = None
    if message.photo:
        image_file_id = message.photo[-1].file_id
    elif not (message.text and message.text.strip().lower() == "/skip"):
        await message.answer("Rasm yuboring yoki /skip yuboring.")
        return

    try:
        question_id = db.add_question(
            grade=int(data["grade"]),
            text=data["text"],
            options=data["options"],
            correct_option=int(data["correct_option"]),
            explanation=data["explanation"],
            source=data["source"],
            image_file_id=image_file_id,
        )
    except Exception as exc:
        logger.exception("Savolni bazaga qo‘shishda xato.")
        await message.answer(f"Savol saqlanmadi: <code>{esc(exc)}</code>")
        return

    await state.clear()
    await message.answer(
        f"Savol qo‘shildi. ID: <code>{question_id}</code>\n"
        f"Sinf: <b>{data['grade']}</b>"
    )


@router.message(Command("setimage"), F.chat.type == ChatType.PRIVATE)
async def set_image_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administrator uchun.")
        return
    await state.clear()
    await state.set_state(SetImage.identifier)
    await message.answer(
        "Rasm biriktiriladigan savolning ID yoki code qiymatini kiriting.\n"
        "Bekor qilish: /cancel"
    )


@router.message(SetImage.identifier)
async def set_image_identifier(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    identifier = (message.text or "").strip()
    question = db.find_question(identifier)
    if not question:
        await message.answer("Bunday savol topilmadi. ID yoki code ni qayta kiriting.")
        return
    await state.update_data(question_id=question["id"], code=question["code"])
    await state.set_state(SetImage.photo)
    await message.answer(
        f"Savol: <code>{esc(question['code'])}</code>\n"
        "Endi rasm yuboring. Rasmni o‘chirish uchun /skip yuboring."
    )


@router.message(SetImage.photo)
async def set_image_photo(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    question_id = int(data["question_id"])

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.text and message.text.strip().lower() == "/skip":
        file_id = None
    else:
        await message.answer("Rasm yuboring yoki /skip yuboring.")
        return

    ok = db.set_question_image(str(question_id), file_id)
    await state.clear()
    if ok:
        action = "o‘chirildi" if file_id is None else "biriktirildi"
        await message.answer(f"Rasm {action}. Savol ID: <code>{question_id}</code>")
    else:
        await message.answer("Savol topilmadi.")


@router.callback_query(F.data.startswith("ans:"))
async def handle_answer(callback: CallbackQuery) -> None:
    data = (callback.data or "").split(":")
    if len(data) != 4:
        await callback.answer("Noto‘g‘ri ma’lumot.", show_alert=True)
        return
    try:
        session_id = int(data[1])
        question_id = int(data[2])
        selected_option = int(data[3])
    except ValueError:
        await callback.answer("Noto‘g‘ri ma’lumot.", show_alert=True)
        return

    user = callback.from_user
    result = db.record_answer(
        session_id=session_id,
        question_id=question_id,
        user_id=user.id,
        display_name=display_name(user),
        username=user.username,
        selected_option=selected_option,
    )

    if result.status == "ok":
        await callback.answer("To‘g‘ri javob." if result.is_correct else "Noto‘g‘ri javob.")
    elif result.status == "duplicate":
        await callback.answer("Siz bu savolga allaqachon javob bergansiz.", show_alert=True)
    elif result.status == "expired":
        await callback.answer("Javob berish vaqti tugadi.", show_alert=True)
    elif result.status == "closed":
        await callback.answer("Bu savol yopilgan.", show_alert=True)
    else:
        await callback.answer("Javob qabul qilinmadi.", show_alert=True)


async def _handle_health_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        pass
    body = b"OK"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    try:
        writer.write(response)
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def start_health_server() -> asyncio.AbstractServer | None:
    raw_port = os.getenv("PORT", "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        logger.warning("PORT noto‘g‘ri: %r. Health server ishga tushmadi.", raw_port)
        return None
    server = await asyncio.start_server(_handle_health_request, "0.0.0.0", port)
    logger.info("Health server %s portda ishga tushdi.", port)
    return server


async def main() -> None:
    global settings, db, bot

    settings = load_settings()
    setup_logging(settings)
    db = Database(settings.db_path)
    db.init_db()
    interrupted = db.interrupt_unfinished_sessions()
    if interrupted:
        logger.warning("Oldingi ishda tugallanmagan %s ta session interrupted qilindi.", interrupted)

    if settings.group_chat_id is not None and db.get_setting("group_chat_id") is None:
        db.set_setting("group_chat_id", str(settings.group_chat_id))

    if not settings.admin_ids:
        logger.warning("ADMIN_IDS bo‘sh. Administrator buyruqlari ishlamaydi.")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        daily_announcement,
        CronTrigger(
            hour=settings.announce_hour,
            minute=settings.announce_minute,
            timezone=settings.timezone,
        ),
        id="daily_announcement",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        daily_quiz,
        CronTrigger(
            hour=settings.quiz_hour,
            minute=settings.quiz_minute,
            timezone=settings.timezone,
        ),
        id="daily_quiz",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=300,
        max_instances=1,
    )

    await set_commands(bot)
    health_server = await start_health_server()
    scheduler.start()
    logger.info(
        "Bot ishga tushdi. Quiz: %02d:%02d, e’lon: %02d:%02d, timezone=%s",
        settings.quiz_hour,
        settings.quiz_minute,
        settings.announce_hour,
        settings.announce_minute,
        settings.timezone,
    )

    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        if background_tasks:
            for task in list(background_tasks):
                task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()
        logger.info("Bot to‘xtatildi.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as error:
        print(f"Sozlama xatosi: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Bot to‘xtatildi.", file=sys.stderr)
