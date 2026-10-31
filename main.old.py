import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import database as db

# ---------------- SOZLAMALAR ----------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID_STR = os.getenv("GROUP_CHAT_ID", "0").strip()
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "").strip()
QUIZ_GRADE = int(os.getenv("QUIZ_GRADE", "8"))
QUESTION_SECONDS = max(20, min(30, int(os.getenv("QUESTION_SECONDS", "25"))))
TZ = ZoneInfo("Asia/Tashkent")
DB_FILE = "quiz.db"

# GROUP_CHAT_ID ni int ga aylantirish
GROUP_CHAT_ID = None
if GROUP_CHAT_ID_STR and GROUP_CHAT_ID_STR != "0":
    try:
        GROUP_CHAT_ID = int(GROUP_CHAT_ID_STR)
    except ValueError:
        GROUP_CHAT_ID = None

# Admin IDs
ADMIN_IDS = set()
if ADMIN_IDS_STR:
    for x in ADMIN_IDS_STR.split(","):
        x = x.strip()
        if x and x.lstrip("-").isdigit():
            ADMIN_IDS.add(int(x))

# Token tekshirish
if not BOT_TOKEN:
    raise RuntimeError(
        "Xatolik: .env faylida BOT_TOKEN topilmadi!\n"
        ".env.example faylidan nusxa olib, BOT_TOKEN ni to'g'ri kiriting."
    )

# Logging sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Bot va Dispatcher
bot = Bot(
    token=BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Scheduler
scheduler = AsyncIOScheduler(timezone=TZ)

# Global o'zgaruvchilar
active_question_id = None
quiz_in_progress = False
quiz_lock = asyncio.Lock()

# ---------------- FSM HOLATLARI ----------------
class AddQuestionFSM(StatesGroup):
    grade = State()
    question = State()
    option_a = State()
    option_b = State()
    option_c = State()
    option_d = State()
    correct_answer = State()
    explanation = State()
    source = State()
    photo = State()

# ---------------- YORDAMCHI FUNKSIYALAR ----------------
def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return False
    return user_id in ADMIN_IDS

def main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Reyting"), KeyboardButton(text="ℹ️ Yordam")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Menuni tanlang"
    )
    return keyboard

def answer_keyboard(question_id: int):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="A", callback_data=f"ans:{question_id}:A"),
                InlineKeyboardButton(text="B", callback_data=f"ans:{question_id}:B"),
            ],
            [
                InlineKeyboardButton(text="C", callback_data=f"ans:{question_id}:C"),
                InlineKeyboardButton(text="D", callback_data=f"ans:{question_id}:D"),
            ]
        ]
    )
    return keyboard

async def send_to_group(text: str, photo_file_id: str = None, reply_markup=None):
    global GROUP_CHAT_ID
    if not GROUP_CHAT_ID:
        logger.warning("GROUP_CHAT_ID sozlanmagan! /setgroup buyrug'ini guruhda yuboring.")
        return False
    try:
        if photo_file_id:
            await bot.send_photo(
                chat_id=GROUP_CHAT_ID,
                photo=photo_file_id,
                caption=text,
                reply_markup=reply_markup
            )
        else:
            await bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=text,
                reply_markup=reply_markup
            )
        return True
    except Exception as e:
        logger.error(f"Guruhga xabar yuborishda xatolik: {e}")
        return False

# ---------------- AVTOMATIK QUIZ FUNKSIYALARI ----------------
async def send_reminder():
    """20:00 da eslatma yuborish"""
    message = (
        "📢 <b>DIQQAT!</b>\n\n"
        "Huquq fanidan quiz <b>5 daqiqadan keyin</b> boshlanadi!\n\n"
        "Tayyorlaning! ⏰"
    )
    await send_to_group(message)
    logger.info("Eslatma xabari yuborildi")

async def start_daily_quiz():
    """20:05 da quizni boshlash"""
    global active_question_id, quiz_in_progress
    
    if not GROUP_CHAT_ID:
        logger.warning("Quizni boshlash mumkin emas: GROUP_CHAT_ID sozlanmagan")
        return
    
    async with quiz_lock:
        if quiz_in_progress:
            logger.info("Quiz allaqachon davom etmoqda")
            return
        
        quiz_in_progress = True
        active_question_id = None
        
        try:
            today = datetime.now(TZ).date().isoformat()
            # Savollarni olish
            questions = db.get_unused_questions(QUIZ_GRADE, today, limit=30)
            
            if not questions:
                await send_to_group(
                    "⚠️ <b>Uzr!</b>\n\n"
                    "Bugun uchun yetarli savollar topilmadi.\n"
                    "Iltimos, admin /addquestion buyrug'i orqali yangi savollar qo'shsin."
                )
                quiz_in_progress = False
                return
            
            # Quiz boshlanishi haqida xabar
            await send_to_group(
                f"⚖️ <b>HUQUQ FANIDAN QUIZ BOSHLANDI!</b>\n\n"
                f"🎓 Sinf: {QUIZ_GRADE}-sinf\n"
                f"📝 Jami savollar soni: {len(questions)} ta\n"
                f"⏱ Har bir savolga: {QUESTION_SECONDS} soniya\n\n"
                f"Javob berishni boshlang! 🚀"
            )
            
            await asyncio.sleep(3)
            
            # Har bir savolni yuborish
            for idx, question in enumerate(questions, 1):
                active_question_id = question['id']
                # Savolni ishlatilgan deb belgilash
                db.mark_question_used(question['id'], today)
                
                # Savol matni
                question_text = (
                    f"📋 <b>{idx}-SAVOL</b> | {question['grade']}-sinf\n\n"
                    f"{question['question']}\n\n"
                    f"A) {question['option_a']}\n"
                    f"B) {question['option_b']}\n"
                    f"C) {question['option_c']}\n"
                    f"D) {question['option_d']}\n\n"
                    f"⏳ <i>Javob berish vaqti: {QUESTION_SECONDS} soniya</i>"
                )
                
                # Savolni yuborish (rasm bilan yoki rasm siz)
                if question['photo_file_id']:
                    await send_to_group(question_text, photo_file_id=question['photo_file_id'], 
                                       reply_markup=answer_keyboard(question['id']))
                else:
                    await send_to_group(question_text, reply_markup=answer_keyboard(question['id']))
                
                # Vaqt kutish
                await asyncio.sleep(QUESTION_SECONDS)
                
                # To'g'ri javobni ko'rsatish
                correct_opt = question['correct_answer']
                correct_text = question[f'option_{correct_opt.lower()}']
                
                result_text = (
                    f"✅ <b>TO'G'RI JAVOB:</b> {correct_opt}) {correct_text}\n\n"
                    f"💡 <b>Tushuntirish:</b>\n{question['explanation']}\n\n"
                    f"📚 <b>Manba:</b> {question['source']}"
                )
                await send_to_group(result_text)
                active_question_id = None
                
                await asyncio.sleep(2)
            
            # Reytingni e'lon qilish
            await asyncio.sleep(3)
            top_scores = db.get_top_scores(10)
            
            if top_scores:
                rating_text = "🏆 <b>BUGUNGI QUIZ NATIJALARI - REYTING</b>\n\n"
                for i, score in enumerate(top_scores, 1):
                    medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                    rating_text += f"{medal} {score['user_name']} — <b>{score['total_score']}</b> ball\n"
                rating_text += "\n👏 Barcha ishtirokchilarga rahmat!"
                await send_to_group(rating_text)
            else:
                await send_to_group("📊 Bugun hech kim to'g'ri javob bermagan.")
            
            await send_to_group("🎉 <b>Bugungi quiz yakunlandi!</b>\n\nErtaga soat 20:00 da yana uchrashamiz! 👋")
            
        except Exception as e:
            logger.error(f"Quiz boshlashda xatolik: {e}")
            await send_to_group("❌ Quiz davomida texnik xatolik yuz berdi.")
        finally:
            quiz_in_progress = False
            active_question_id = None

# ---------------- BUYRUQLAR HANDLERLARI ----------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "⚖️ Men <b>Huquq fanidan Quiz Bot</b>man.\n\n"
        "Har kuni soat <b>20:00</b> da eslatma, <b>20:05</b> da esa quiz boshlanadi.\n\n"
        "Buyruqlar:\n"
        "/rating - Umumiy reytingni ko'rish\n"
        "/help - Yordam",
        reply_markup=main_keyboard()
    )

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Yordam")
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>YORDAM</b>\n\n"
        "<b>Foydalanuvchilar uchun:</b>\n"
        "/start - Botni ishga tushirish\n"
        "/rating - Reytingni ko'rish\n"
        "/id - Chat ID ni ko'rish\n\n"
        "<b>Adminlar uchun:</b>\n"
        "/setgroup - Guruhni quiz uchun sozlash (guruh ichida)\n"
        "/addquestion - Yangi savol qo'shish\n"
        "/quiznow - Quizni hozir boshlash\n"
        "/cancel - Savol qo'shishni bekor qilish\n\n"
        "💡 <b>Maslahat:</b> Quizda javob berish uchun A, B, C, D tugmalarini bosing."
    )
    await message.answer(help_text, reply_markup=main_keyboard())

@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 Bu chatning ID si:\n<code>{message.chat.id}</code>")

@router.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    global GROUP_CHAT_ID
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("⚠️ Bu buyruqni faqat <b>guruh</b> ichida yuboring!")
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat bot adminlari uchun!")
        return
    
    GROUP_CHAT_ID = message.chat.id
    db.set_setting("group_chat_id", str(GROUP_CHAT_ID))
    await message.answer(
        f"✅ Guruh muvaffaqiyatli sozlandi!\n\n"
        f"Chat ID: <code>{GROUP_CHAT_ID}</code>\n\n"
        f"Endi har kuni soat 20:00 da quiz o'tkaziladi."
    )
    logger.info(f"Guruh sozlandi: {GROUP_CHAT_ID} by {message.from_user.id}")

@router.message(Command("rating"))
@router.message(F.text == "📊 Reyting")
async def cmd_rating(message: Message):
    top_scores = db.get_top_scores(15)
    if not top_scores:
        await message.answer(
            "📊 <b>Reyting</b>\n\n"
            "Hozircha hech kim ball to'plamagan.\n"
            "Birinchi bo'lib quizda ishtirok eting!"
        )
        return
    
    rating_text = "🏆 <b>UMUMIY REYTING</b>\n\n"
    for i, score in enumerate(top_scores, 1):
        if i == 1:
            medal = "🥇"
        elif i == 2:
            medal = "🥈"
        elif i == 3:
            medal = "🥉"
        else:
            medal = f"{i}."
        rating_text += f"{medal} {score['user_name']} — <b>{score['total_score']}</b> ball\n"
    
    await message.answer(rating_text, reply_markup=main_keyboard())

@router.message(Command("quiznow"))
async def cmd_quiznow(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat bot adminlari uchun!")
        return
    if quiz_in_progress:
        await message.answer("⚠️ Quiz allaqachon davom etmoqda!")
        return
    await message.answer("🚀 Quiz hozir boshlanadi...")
    await start_daily_quiz()

# ---------------- JAVOB BERISH ----------------
@router.callback_query(F.data.startswith("ans:"))
async def process_answer(callback: CallbackQuery):
    global active_question_id
    
    try:
        data_parts = callback.data.split(":")
        question_id = int(data_parts[1])
        user_answer = data_parts[2]
    except:
        await callback.answer("Xatolik yuz berdi!", show_alert=True)
        return
    
    # Savol hali faolmi tekshirish
    if active_question_id != question_id:
        await callback.answer("⏰ Bu savolga javob berish vaqti tugagan!", show_alert=True)
        return
    
    question = db.get_question_by_id(question_id)
    if not question:
        await callback.answer("Savol topilmadi!", show_alert=True)
        return
    
    user = callback.from_user
    user_name = user.full_name
    if user.username:
        user_name = f"{user.full_name} (@{user.username})"
    
    is_correct = (user_answer == question['correct_answer'])
    
    # Javobni saqlash
    saved = db.save_user_answer(question_id, user.id, user_name, user_answer, is_correct)
    
    if not saved:
        await callback.answer("⚠️ Siz bu savolga allaqachon javob bergansiz!", show_alert=True)
        return
    
    if is_correct:
        await callback.answer("✅ To'g'ri javob! +1 ball qo'shildi 🎉", show_alert=True)
    else:
        await callback.answer("❌ Noto'g'ri javob!", show_alert=True)

# ---------------- SAVOL QO'SHISH FSM ----------------
@router.message(Command("addquestion"))
async def add_question_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat bot adminlari uchun!")
        return
    await state.clear()
    await state.set_state(AddQuestionFSM.grade)
    await message.answer(
        "📝 <b>YANGI SAVOL QO'SHISH</b>\n\n"
        "Qaysi sinf uchun savol qo'shmoqchisiz?\n"
        "<i>(1 dan 11 gacha raqam kiriting)</i>"
    )

@router.message(AddQuestionFSM.grade)
async def add_question_grade(message: Message, state: FSMContext):
    try:
        grade = int(message.text.strip())
        if grade < 1 or grade > 11:
            raise ValueError
        await state.update_data(grade=grade)
        await state.set_state(AddQuestionFSM.question)
        await message.answer("Savol matnini to'liq kiriting:")
    except ValueError:
        await message.answer("❌ Iltimos, 1 dan 11 gacha to'g'ri raqam kiriting!")

@router.message(AddQuestionFSM.question)
async def add_question_text(message: Message, state: FSMContext):
    await state.update_data(question=message.text.strip())
    await state.set_state(AddQuestionFSM.option_a)
    await message.answer("<b>A)</b> variantini kiriting:")

@router.message(AddQuestionFSM.option_a)
async def add_option_a(message: Message, state: FSMContext):
    await state.update_data(option_a=message.text.strip())
    await state.set_state(AddQuestionFSM.option_b)
    await message.answer("<b>B)</b> variantini kiriting:")

@router.message(AddQuestionFSM.option_b)
async def add_option_b(message: Message, state: FSMContext):
    await state.update_data(option_b=message.text.strip())
    await state.set_state(AddQuestionFSM.option_c)
    await message.answer("<b>C)</b> variantini kiriting:")

@router.message(AddQuestionFSM.option_c)
async def add_option_c(message: Message, state: FSMContext):
    await state.update_data(option_c=message.text.strip())
    await state.set_state(AddQuestionFSM.option_d)
    await message.answer("<b>D)</b> variantini kiriting:")

@router.message(AddQuestionFSM.option_d)
async def add_option_d(message: Message, state: FSMContext):
    await state.update_data(option_d=message.text.strip())
    await state.set_state(AddQuestionFSM.correct_answer)
    await message.answer("To'g'ri javobni tanlang: <b>A</b>, <b>B</b>, <b>C</b> yoki <b>D</b>")

@router.message(AddQuestionFSM.correct_answer)
async def add_correct_answer(message: Message, state: FSMContext):
    correct = message.text.strip().upper()
    if correct not in ['A', 'B', 'C', 'D']:
        await message.answer("❌ Faqat A, B, C yoki D harflaridan birini kiriting!")
        return
    await state.update_data(correct_answer=correct)
    await state.set_state(AddQuestionFSM.explanation)
    await message.answer("Qisqa tushuntirishni kiriting (to'g'ri javob nima uchun to'g'ri):")

@router.message(AddQuestionFSM.explanation)
async def add_explanation(message: Message, state: FSMContext):
    await state.update_data(explanation=message.text.strip())
    await state.set_state(AddQuestionFSM.source)
    await message.answer("Manbani kiriting (darslik nomi, bob nomi yoki rasmiy manba):")

@router.message(AddQuestionFSM.source)
async def add_source(message: Message, state: FSMContext):
    await state.update_data(source=message.text.strip())
    await state.set_state(AddQuestionFSM.photo)
    await message.answer(
        "Rasm qo'shmoqchimisiz?\n\n"
        "Agar rasm qo'shmoqchi bo'lsangiz - rasmni yuboring.\n"
        "Agar rasm qo'shmaslikni istasangiz - <b>/skip</b> buyrug'ini yuboring."
    )

@router.message(AddQuestionFSM.photo, Command("skip"))
async def add_question_skip_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        db.add_question(
            grade=data['grade'],
            question=data['question'],
            option_a=data['option_a'],
            option_b=data['option_b'],
            option_c=data['option_c'],
            option_d=data['option_d'],
            correct_answer=data['correct_answer'],
            explanation=data['explanation'],
            source=data['source'],
            photo_file_id=None
        )
        await state.clear()
        await message.answer("✅ Savol muvaffaqiyatli bazaga qo'shildi!")
    except Exception as e:
        logger.error(f"Savol qo'shishda xatolik: {e}")
        await message.answer("❌ Savol qo'shishda xatolik yuz berdi!")
        await state.clear()

@router.message(AddQuestionFSM.photo, F.photo)
async def add_question_with_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photo = message.photo[-1]
    try:
        db.add_question(
            grade=data['grade'],
            question=data['question'],
            option_a=data['option_a'],
            option_b=data['option_b'],
            option_c=data['option_c'],
            option_d=data['option_d'],
            correct_answer=data['correct_answer'],
            explanation=data['explanation'],
            source=data['source'],
            photo_file_id=photo.file_id
        )
        await state.clear()
        await message.answer("✅ Rasmli savol muvaffaqiyatli bazaga qo'shildi!")
    except Exception as e:
        logger.error(f"Savol qo'shishda xatolik: {e}")
        await message.answer("❌ Savol qo'shishda xatolik yuz berdi!")
        await state.clear()

@router.message(AddQuestionFSM.photo)
async def add_question_photo_wrong(message: Message):
    await message.answer("❌ Iltimos, rasm yuboring yoki /skip buyrug'ini yuboring!")

@router.message(Command("cancel"))
async def cancel_state(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Bekor qilinadigan jarayon yo'q.")
        return
    await state.clear()
    await message.answer("✅ Jarayon bekor qilindi.")

# ---------------- ISHGA TUSHIRISH ----------------
async def on_startup():
    """Bot ishga tushganda bajariladigan ishlar"""
    logger.info("Bot ishga tushmoqda...")
    # Bazani yaratish
    db.init_db()
    logger.info("Ma'lumotlar bazasi tayyorlandi")
    
    # Saqlangan GROUP_CHAT_ID ni yuklash
    global GROUP_CHAT_ID
    saved_group = db.get_setting("group_chat_id")
    if saved_group and not GROUP_CHAT_ID:
        try:
            GROUP_CHAT_ID = int(saved_group)
            logger.info(f"Saqlangan guruh yuklandi: {GROUP_CHAT_ID}")
        except ValueError:
            pass
    
    # Scheduler ni sozlash
    # Har kuni soat 20:00 da eslatma
    scheduler.add_job(send_reminder, 'cron', hour=20, minute=0, timezone=TZ)
    # Har kuni soat 20:05 da quiz boshlash
    scheduler.add_job(start_daily_quiz, 'cron', hour=20, minute=5, timezone=TZ)
    
    scheduler.start()
    logger.info("Scheduler ishga tushdi - Eslatma: 20:00, Quiz: 20:05 (Asia/Tashkent)")
    logger.info("Bot muvaffaqiyatli ishga tushdi!")

async def on_shutdown():
    """Bot to'xtaganda bajariladigan ishlar"""
    logger.info("Bot to'xtatilmoqda...")
    if scheduler.running:
        scheduler.shutdown()
    await bot.session.close()
    logger.info("Bot to'xtatildi")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
         