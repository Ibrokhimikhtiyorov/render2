import os, textwrap, zipfile, py_compile, pathlib

base = "/mnt/data/PUBG-Garant-Bot"
os.makedirs(base, exist_ok=True)

files = {}

files["config.py"] = r'''
import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} .env faylida ko'rsatilmagan")
    return value


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} butun son bo'lishi kerak") from exc
    if value < minimum:
        raise ConfigError(f"{name} kamida {minimum} bo'lishi kerak")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    admin_ids: set[int]
    fee_percent: int
    min_amount: int
    review_minutes: int
    admin_card: str


def load_settings() -> Settings:
    token = _required("BOT_TOKEN")
    database_url = _required("DATABASE_URL")
    raw_admins = _required("ADMIN_IDS")
    try:
        admin_ids = {int(x.strip()) for x in raw_admins.split(",") if x.strip()}
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS faqat Telegram ID raqamlaridan iborat bo'lsin") from exc
    if not admin_ids:
        raise ConfigError("ADMIN_IDS ichida kamida bitta admin ID bo'lishi kerak")
    fee = _int("FEE_PERCENT", 5, 0)
    if fee > 30:
        raise ConfigError("FEE_PERCENT 30 dan katta bo'lmasin")
    return Settings(
        bot_token=token,
        database_url=database_url,
        admin_ids=admin_ids,
        fee_percent=fee,
        min_amount=_int("MIN_AMOUNT", 10000, 1),
        review_minutes=_int("REVIEW_MINUTES", 30, 1),
        admin_card=os.getenv("ADMIN_CARD", "Admin bilan bog'laning").strip(),
    )


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
'''

files["database.py"] = r'''
import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5, command_timeout=30)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database ulanmagan")
        return self.pool

    async def init_db(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            accepted BOOLEAN NOT NULL DEFAULT FALSE,
            role TEXT,
            balance BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS deals (
            id BIGSERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            seller_id BIGINT NOT NULL REFERENCES users(user_id),
            buyer_id BIGINT REFERENCES users(user_id),
            description TEXT NOT NULL,
            photos JSONB NOT NULL DEFAULT '[]'::jsonb,
            price BIGINT NOT NULL CHECK(price > 0),
            seller_phone TEXT NOT NULL,
            buyer_phone TEXT,
            transfer_note TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'listed',
            fee BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            UNIQUE(code)
        );
        CREATE TABLE IF NOT EXISTS deposits (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(user_id),
            amount BIGINT NOT NULL,
            photo_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(user_id),
            amount BIGINT NOT NULL,
            card TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS deals_status_idx ON deals(status);
        CREATE INDEX IF NOT EXISTS deals_buyer_idx ON deals(buyer_id);
        """
        await self._pool().execute(sql)

    async def ensure_user(self, user_id: int, username: str | None) -> None:
        await self._pool().execute(
            """INSERT INTO users(user_id, username) VALUES($1,$2)
               ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username""",
            user_id, username,
        )

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return dict(row) if row else None

    async def set_accepted(self, user_id: int, accepted: bool) -> None:
        await self._pool().execute("UPDATE users SET accepted=$2 WHERE user_id=$1", user_id, accepted)

    async def set_role(self, user_id: int, role: str) -> None:
        await self._pool().execute("UPDATE users SET role=$2 WHERE user_id=$1", user_id, role)

    async def get_balance(self, user_id: int) -> int:
        value = await self._pool().fetchval("SELECT balance FROM users WHERE user_id=$1", user_id)
        return int(value or 0)

    async def create_deal(self, seller_id: int, description: str, photos: list[str],
                          price: int, phone: str, transfer_note: str) -> dict[str, Any] | None:
        pool = self._pool()
        alphabet = string.digits
        async with pool.acquire() as conn:
            for _ in range(30):
                code = "".join(secrets.choice(alphabet) for _ in range(4))
                try:
                    row = await conn.fetchrow(
                        """INSERT INTO deals(code,seller_id,description,photos,price,seller_phone,transfer_note)
                           VALUES($1,$2,$3,$4::jsonb,$5,$6,$7) RETURNING *""",
                        code, seller_id, description, json.dumps(photos), price, phone, transfer_note,
                    )
                    return dict(row)
                except asyncpg.UniqueViolationError:
                    continue
        return None

    async def join_deal(self, code: str, buyer_id: int) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """UPDATE deals SET buyer_id=$2,status='joined'
               WHERE code=$1 AND status='listed' AND seller_id<>$2 RETURNING *""",
            code, buyer_id,
        )
        return dict(row) if row else None

    async def set_buyer_phone(self, deal_id: int, buyer_id: int, phone: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """UPDATE deals SET buyer_phone=$3 WHERE id=$1 AND buyer_id=$2 AND status='joined'
               RETURNING *""", deal_id, buyer_id, phone,
        )
        return dict(row) if row else None

    async def pay_deal(self, deal_id: int, buyer_id: int, review_minutes: int):
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM deals WHERE id=$1 FOR UPDATE", deal_id
                )
                if not row or row["buyer_id"] != buyer_id or row["status"] != "joined":
                    return "missing", None
                user = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", buyer_id)
                if not user or user["balance"] < row["price"]:
                    return "balance", dict(row)
                await conn.execute("UPDATE users SET balance=balance-$2 WHERE user_id=$1", buyer_id, row["price"])
                updated = await conn.fetchrow(
                    """UPDATE deals SET status='paid', expires_at=$2
                       WHERE id=$1 RETURNING *""",
                    deal_id, datetime.now(timezone.utc) + timedelta(minutes=review_minutes),
                )
                return "ok", dict(updated)

    async def settle(self, deal_id: int, decision: str, fee_percent: int) -> dict[str, Any] | None:
        """decision: release (seller gets money) or refund (buyer gets money)."""
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM deals WHERE id=$1 FOR UPDATE", deal_id)
                if not row or row["status"] not in ("paid", "dispute"):
                    return None
                if decision == "release":
                    fee = int(row["price"] * fee_percent / 100)
                    seller_amount = row["price"] - fee
                    await conn.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["seller_id"], seller_amount)
                    status = "completed"
                elif decision == "refund":
                    fee = 0
                    await conn.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["buyer_id"], row["price"])
                    status = "refunded"
                else:
                    raise ValueError("Noto'g'ri qaror")
                updated = await conn.fetchrow(
                    "UPDATE deals SET status=$2, fee=$3, expires_at=NULL WHERE id=$1 RETURNING *",
                    deal_id, status, fee,
                )
                return dict(updated)

    async def mark_dispute(self, deal_id: int, buyer_id: int, reason: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """UPDATE deals SET status='dispute', expires_at=NULL
               WHERE id=$1 AND buyer_id=$2 AND status='paid' RETURNING *""",
            deal_id, buyer_id,
        )
        return dict(row) if row else None

    async def expired_deal_ids(self) -> list[int]:
        rows = await self._pool().fetch(
            "SELECT id FROM deals WHERE status='paid' AND expires_at <= NOW()"
        )
        return [int(r["id"]) for r in rows]

    async def cancel_deal(self, deal_id: int, seller_id: int) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """UPDATE deals SET status='cancelled'
               WHERE id=$1 AND seller_id=$2 AND status IN ('listed','joined') RETURNING *""",
            deal_id, seller_id,
        )
        return dict(row) if row else None

    async def user_deals(self, user_id: int) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM deals WHERE seller_id=$1 OR buyer_id=$1 ORDER BY id DESC LIMIT 20", user_id
        )
        return [dict(r) for r in rows]

    async def create_deposit(self, user_id: int, amount: int, photo_id: str) -> int:
        return int(await self._pool().fetchval(
            "INSERT INTO deposits(user_id,amount,photo_id) VALUES($1,$2,$3) RETURNING id",
            user_id, amount, photo_id,
        ))

    async def resolve_deposit(self, deposit_id: int, approve: bool):
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM deposits WHERE id=$1 FOR UPDATE", deposit_id)
                if not row or row["status"] != "pending":
                    return None
                status = "approved" if approve else "rejected"
                await conn.execute("UPDATE deposits SET status=$2 WHERE id=$1", deposit_id, status)
                if approve:
                    await conn.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["user_id"], row["amount"])
                return dict(row)

    async def request_withdrawal(self, user_id: int, amount: int, card: str) -> int | None:
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", user_id)
                if not user or user["balance"] < amount:
                    return None
                await conn.execute("UPDATE users SET balance=balance-$2 WHERE user_id=$1", user_id, amount)
                return int(await conn.fetchval(
                    "INSERT INTO withdrawals(user_id,amount,card) VALUES($1,$2,$3) RETURNING id",
                    user_id, amount, card,
                ))

    async def resolve_withdrawal(self, wd_id: int, paid: bool):
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE", wd_id)
                if not row or row["status"] != "pending":
                    return None
                status = "paid" if paid else "rejected"
                await conn.execute("UPDATE withdrawals SET status=$2 WHERE id=$1", wd_id, status)
                if not paid:
                    await conn.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["user_id"], row["amount"])
                return dict(row)

    async def stats(self) -> dict[str, int]:
        row = await self._pool().fetchrow(
            """SELECT COUNT(*) FILTER(WHERE status='completed') AS n,
                      COALESCE(SUM(fee) FILTER(WHERE status='completed'),0) AS fees
               FROM deals"""
        )
        return {"n": int(row["n"]), "fees": int(row["fees"])}
'''

files["handlers.py"] = r'''
import asyncio
import html
import logging
import re
import time
from typing import Any

from aiogram import F, Router, Bot
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto,
)

from database import Database
from config import Settings

log = logging.getLogger(__name__)
router = Router()
PHONE_RE = re.compile(r"^\+?\d{9,15}$")

SELL = "💰 Akkaunt sotaman"
BUY = "🛒 Akkaunt olaman"
BAL = "💳 Balans"
DEALS = "📋 Savdolarim"
CANCEL = "❌ Bekor qilish"
DONE = "✅ Tayyor"

class Sell(StatesGroup):
    desc = State()
    photos = State()
    price = State()
    phone = State()
    transfer = State()

class Buy(StatesGroup):
    code = State()
    phone = State()

class Deposit(StatesGroup):
    amount = State()
    photo = State()

class Withdraw(StatesGroup):
    amount = State()
    card = State()

def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)

def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " so'm"

def reply_kb(*labels: str):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=x)] for x in labels], resize_keyboard=True)

def inline(*buttons: tuple[str, str]):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons]
    ])

def menu(role: str):
    if role == "seller":
        rows = [[SELL], [DEALS, BAL], ["🔄 Rolni almashtirish"]]
    elif role == "buyer":
        rows = [[BUY], [DEALS, BAL], ["🔄 Rolni almashtirish"]]
    else:
        rows = [[SELL, BUY], [BAL, DEALS]]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True
    )

def valid_amount(text: str, minimum: int) -> int | None:
    raw = text.replace(" ", "").replace(",", "")
    return int(raw) if raw.isdigit() and int(raw) >= minimum else None

class App:
    def __init__(self, bot: Bot, db: Database, settings: Settings):
        self.bot, self.db, self.settings = bot, db, settings
        self.code_attempts: dict[int, list[float]] = {}

    def admin(self, uid: int) -> bool:
        return uid in self.settings.admin_ids

    async def notify_admins(self, text: str, markup=None, photo=None):
        for admin in self.settings.admin_ids:
            try:
                if photo:
                    await self.bot.send_photo(admin, photo, caption=text, reply_markup=markup)
                else:
                    await self.bot.send_message(admin, text, reply_markup=markup)
            except Exception:
                log.exception("Admin xabari yuborilmadi")

    async def send_user(self, uid: int, text: str):
        try:
            await self.bot.send_message(uid, text)
        except Exception:
            log.exception("Foydalanuvchiga xabar yuborilmadi: %s", uid)

    async def start(self, message: Message, state: FSMContext):
        await state.clear()
        user = message.from_user
        await self.db.ensure_user(user.id, user.username)
        record = await self.db.get_user(user.id)
        if not record["accepted"]:
            await message.answer(
                "<b>🎮 PUBG GARANT — RASMIY SAVDO QOIDALARI</b>\n\n"
                "1. Bot akkaunt savdosida tomonlar o'rtasida vositachi bo'ladi.\n"
                "2. Xaridor to'lovi savdo tekshiruvi tugaguncha bot balansida ushlab turiladi.\n"
                "3. Xaridor fikridan qaytishi yoki sababsiz rad etishi pulni qaytarish uchun asos emas.\n"
                "4. Sotuvchi akkauntni bermasa, ma'lumotlar ishlamasa yoki e'lon tavsifiga mos kelmasa, xaridor nizo ochishi mumkin.\n"
                "5. Nizoni admin dalillar asosida ko'rib chiqadi. Firibgarlik yoki sotuvchi aybi tasdiqlansa, pul xaridorga qaytariladi; aks holda sotuvchiga o'tkaziladi.\n"
                f"6. Savdo muvaffaqiyatli yakunlansa, komissiya {self.settings.fee_percent}%.\n"
                f"7. Tekshirish muddati {self.settings.review_minutes} daqiqa. Shu muddatda xaridor tasdiqlashi yoki nizo ochishi kerak.\n"
                "8. To'lov cheki va yozishmalarni saqlang. Admin qarori uchun dalillar talab qilinishi mumkin.\n\n"
                "<b>DIQQAT:</b> Bot shaxsiy Gmail parolingizni so'ramaydi. Faqat PUBG akkauntini topshirish uchun zarur, alohida ajratilgan ma'lumotlardan foydalaning. Hech qachon bank, Telegram yoki shaxsiy email parolingizni yubormang.\n\n"
                "Davom etish orqali ushbu qoidalarni o'qiganingizni va qabul qilganingizni tasdiqlaysiz.",
                reply_markup=inline(("✅ Roziman", "agree"), ("❌ Rozi emasman", "disagree"))
            )
            return
        await self.show_role(message)

    async def show_role(self, message: Message):
        user = await self.db.get_user(message.from_user.id)
        if not user or not user["role"]:
            await message.answer("Siz kim sifatida foydalanmoqchisiz?", reply_markup=inline(
                ("💰 Sotuvchi", "role:seller"), ("🛒 Xaridor", "role:buyer")
            ))
            return
        await message.answer("Asosiy menyu:", reply_markup=menu(user["role"]))

    async def agree(self, callback: CallbackQuery):
        await self.db.set_accepted(callback.from_user.id, True)
        await callback.answer("Qoidalar qabul qilindi")
        if callback.message:
            await callback.message.edit_text("✅ Qoidalar qabul qilindi.")
            await callback.message.answer("Siz kim sifatida foydalanmoqchisiz?", reply_markup=inline(
                ("💰 Sotuvchi", "role:seller"), ("🛒 Xaridor", "role:buyer")
            ))

    async def disagree(self, callback: CallbackQuery, state: FSMContext):
        await self.db.set_accepted(callback.from_user.id, False)
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.edit_text("Qoidalar qabul qilinmadi. Xayr, sog' bo'ling! 👋")

    async def choose_role(self, callback: CallbackQuery):
        role = callback.data.split(":")[1]
        await self.db.set_role(callback.from_user.id, role)
        await callback.answer("Rol saqlandi")
        if callback.message:
            await callback.message.edit_text("✅ Rol tanlandi.")
            await callback.message.answer("Asosiy menyu:", reply_markup=menu(role))

    async def sell_start(self, message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Sell.desc)
        await message.answer("Akkaunt haqida yozing: level, rank, skinlar va holati.", reply_markup=reply_kb(CANCEL))

    async def sell_desc(self, message: Message, state: FSMContext):
        if not message.text or len(message.text.strip()) < 10:
            await message.answer("Kamida 10 ta belgi bilan batafsil yozing.")
            return
        await state.update_data(desc=message.text.strip()[:1000], photos=[])
        await state.set_state(Sell.photos)
        await message.answer("1–10 ta skrinshot yuboring. Tugagach «✅ Tayyor» bosing.", reply_markup=reply_kb(DONE, CANCEL))

    async def sell_photo(self, message: Message, state: FSMContext):
        data = await state.get_data()
        photos = data.get("photos", [])
        if len(photos) >= 10:
            await message.answer("Maksimum 10 ta rasm.")
            return
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        await message.answer(f"Rasm qabul qilindi: {len(photos)}/10")

    async def sell_photos_done(self, message: Message, state: FSMContext):
        data = await state.get_data()
        if not data.get("photos"):
            await message.answer("Kamida bitta rasm yuboring.")
            return
        await state.set_state(Sell.price)
        await message.answer("Narxni so'mda kiriting (masalan: 500000).", reply_markup=reply_kb(CANCEL))

    async def sell_price(self, message: Message, state: FSMContext):
        amount = valid_amount(message.text or "", self.settings.min_amount)
        if amount is None:
            await message.answer(f"Eng kam summa: {money(self.settings.min_amount)}")
            return
        await state.update_data(price=amount)
        await state.set_state(Sell.phone)
        await message.answer("Aloqa telefon raqamingizni kiriting (+998...).")

    async def sell_phone(self, message: Message, state: FSMContext):
        phone = (message.text or "").replace(" ", "")
        if not PHONE_RE.fullmatch(phone):
            await message.answer("Telefon noto'g'ri. Masalan: +998901234567")
            return
        await state.update_data(phone=phone)
        await state.set_state(Sell.transfer)
        await message.answer(
            "Akkauntni topshirish tartibini yozing (masalan, PUBG ID va topshirish bo'yicha izoh). "
            "Shaxsiy Gmail/bank/Telegram parolingizni yubormang."
        )

    async def sell_transfer(self, message: Message, state: FSMContext):
        if not message.text or len(message.text.strip()) < 5:
            await message.answer("Topshirish tartibini batafsilroq yozing.")
            return
        data = await state.get_data()
        deal = await self.db.create_deal(
            message.from_user.id, data["desc"], data["photos"], data["price"],
            data["phone"], message.text.strip()[:1000]
        )
        await state.clear()
        if not deal:
            await message.answer("Kod yaratilmadi. Qayta urinib ko'ring.", reply_markup=menu("seller"))
            return
        await message.answer(
            f"✅ E'lon yaratildi: <b>#{deal['id']}</b>\nNarx: <b>{money(deal['price'])}</b>\n"
            f"Xaridorga ushbu kodni bering: <code>{deal['code']}</code>",
            reply_markup=menu("seller")
        )

    async def buy_start(self, message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Buy.code)
        await message.answer("Sotuvchi bergan 4 xonali kodni kiriting.", reply_markup=reply_kb(CANCEL))

    async def buy_code(self, message: Message, state: FSMContext):
        code = (message.text or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            await message.answer("Kod 4 ta raqamdan iborat bo'lishi kerak.")
            return
        now = time.monotonic()
        attempts = [x for x in self.code_attempts.get(message.from_user.id, []) if now-x < 600]
        if len(attempts) >= 5:
            await message.answer("Ko'p urinish. 10 daqiqadan keyin urinib ko'ring.")
            return
        deal = await self.db.join_deal(code, message.from_user.id)
        if not deal:
            attempts.append(now)
            self.code_attempts[message.from_user.id] = attempts
            await message.answer("Kod noto'g'ri yoki e'lon band.")
            return
        await state.update_data(deal_id=deal["id"])
        await state.set_state(Buy.phone)
        await message.answer("Telefon raqamingizni kiriting (+998...).")

    async def buy_phone(self, message: Message, state: FSMContext):
        phone = (message.text or "").replace(" ", "")
        if not PHONE_RE.fullmatch(phone):
            await message.answer("Telefon noto'g'ri. Masalan: +998901234567")
            return
        deal_id = (await state.get_data())["deal_id"]
        deal = await self.db.set_buyer_phone(deal_id, message.from_user.id, phone)
        await state.clear()
        if not deal:
            await message.answer("Savdo topilmadi yoki bekor qilingan.", reply_markup=menu("buyer"))
            return
        media = [InputMediaPhoto(media=p) for p in deal["photos"]]
        await message.answer_media_group(media)
        await message.answer(
            f"🎮 <b>Akkaunt #{deal['id']}</b>\n{esc(deal['description'])}\n\n"
            f"💵 Narx: <b>{money(deal['price'])}</b>\n📞 Sotuvchi: {esc(deal['seller_phone'])}\n"
            f"⏳ To'lovdan keyin tekshirish: {self.settings.review_minutes} daqiqa.",
            reply_markup=inline(("💳 Balansdan to'lash", f"pay:{deal['id']}"))
        )
        await message.answer("Menyu:", reply_markup=menu("buyer"))
        await self.send_user(deal["seller_id"], f"Xaridor #{deal['id']} savdoga ulandi. Xaridor telefoni: {phone}")

    async def pay(self, callback: CallbackQuery):
        try:
            deal_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Noto'g'ri savdo.", show_alert=True)
            return
        status, deal = await self.db.pay_deal(deal_id, callback.from_user.id, self.settings.review_minutes)
        if status == "missing":
            await callback.answer("Savdo topilmadi yoki yopilgan.", show_alert=True)
            return
        if status == "balance":
            await callback.answer(f"Balans yetarli emas: {money(deal['price'])}", show_alert=True)
            return
        await callback.answer("To'lov muvaffaqiyatli")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ To'lov bot balansidan ushlab qolindi.\n"
            f"Sotuvchi topshirish bo'yicha izoh: {esc(deal['transfer_note'])}\n\n"
            "Akkauntni tekshiring. Hammasi to'g'ri bo'lsa tasdiqlang. Muammo bo'lsa nizo oching.",
            reply_markup=inline(("✅ Qabul qildim", f"ok:{deal_id}"), ("⚠️ Nizo ochish", f"dispute:{deal_id}"))
        )
        await self.send_user(deal["seller_id"], f"💳 #{deal_id} savdosi to'landi. Xaridor tekshiryapti. Akkaunt topshirish bo'yicha aloqada bo'ling.")

    async def buyer_decision(self, callback: CallbackQuery):
        action, raw = callback.data.split(":")
        deal_id = int(raw)
        if action == "ok":
            deal = await self.db.settle(deal_id, "release", self.settings.fee_percent)
            if not deal:
                await callback.answer("Savdo allaqachon ko'rib chiqilmoqda/yopilgan.", show_alert=True)
                return
            await callback.answer("Savdo yakunlandi")
            await self.send_user(deal["seller_id"], f"✅ #{deal_id} yakunlandi. Balansingizga {money(deal['price']-deal['fee'])} tushdi. Komissiya: {money(deal['fee'])}.")
            await self.send_user(deal["buyer_id"], "✅ Savdo yakunlandi. Rahmat!")
            if callback.message:
                await callback.message.edit_text("✅ Savdo xaridor tomonidan tasdiqlandi.")
        else:
            deal = await self.db.mark_dispute(deal_id, callback.from_user.id, "Xaridor nizo ochdi")
            if not deal:
                await callback.answer("Nizo ochib bo'lmadi.", show_alert=True)
                return
            await callback.answer("Nizo adminga yuborildi")
            if callback.message:
                await callback.message.edit_text("⚠️ Nizo ochildi. Admin tekshiruvini kuting.")
            await self.notify_admins(
                f"⚠️ <b>NIZO #{deal_id}</b>\nSotuvchi: {deal['seller_id']}\nXaridor: {deal['buyer_id']}\n"
                f"Summa: {money(deal['price'])}",
                inline(("💰 Sotuvchiga o'tkazish", f"resolve_release:{deal_id}"),
                       ("↩️ Xaridorga qaytarish", f"resolve_refund:{deal_id}"))
            )
            await self.send_user(deal["seller_id"], f"⚠️ #{deal_id} bo'yicha xaridor nizo ochdi. Admin tekshirmoqda.")

    async def resolve_dispute(self, callback: CallbackQuery):
        if not self.admin(callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        action, raw = callback.data.split(":")
        decision = "release" if action == "resolve_release" else "refund"
        deal = await self.db.settle(int(raw), decision, self.settings.fee_percent)
        if not deal:
            await callback.answer("Nizo allaqachon hal qilingan.", show_alert=True)
            return
        await callback.answer("Qaror saqlandi")
        if callback.message:
            await callback.message.edit_text((callback.message.text or "") + "\n\n✅ Admin qarori bajarildi.")
        if decision == "refund":
            await self.send_user(deal["buyer_id"], f"↩️ #{deal['id']} bo'yicha {money(deal['price'])} balansingizga qaytarildi.")
            await self.send_user(deal["seller_id"], f"❌ #{deal['id']} bo'yicha admin pulni xaridorga qaytarishga qaror qildi.")
        else:
            await self.send_user(deal["seller_id"], f"✅ #{deal['id']} bo'yicha {money(deal['price']-deal['fee'])} balansingizga o'tkazildi.")
            await self.send_user(deal["buyer_id"], f"Admin nizoni sotuvchi foydasiga hal qildi. Savdo yakunlandi.")

    async def balance(self, message: Message, state: FSMContext):
        await state.clear()
        amount = await self.db.get_balance(message.from_user.id)
        await message.answer(f"💳 Balansingiz: <b>{money(amount)}</b>", reply_markup=inline(
            ("➕ To'ldirish", "dep"), ("➖ Yechish", "wd")
        ))

    async def deposit_start(self, callback: CallbackQuery, state: FSMContext):
        await state.set_state(Deposit.amount)
        await callback.message.answer("To'ldirish summasini kiriting:", reply_markup=reply_kb(CANCEL))
        await callback.answer()

    async def deposit_amount(self, message: Message, state: FSMContext):
        amount = valid_amount(message.text or "", self.settings.min_amount)
        if amount is None:
            await message.answer(f"Eng kam summa {money(self.settings.min_amount)}")
            return
        await state.update_data(amount=amount)
        await state.set_state(Deposit.photo)
        await message.answer(f"{money(amount)} ni ushbu kartaga o'tkazing:\n<code>{esc(self.settings.admin_card)}</code>\nChek rasmini yuboring.")

    async def deposit_photo(self, message: Message, state: FSMContext):
        amount = (await state.get_data())["amount"]
        dep_id = await self.db.create_deposit(message.from_user.id, amount, message.photo[-1].file_id)
        await state.clear()
        await self.notify_admins(
            f"💵 To'ldirish #{dep_id}\nFoydalanuvchi: {message.from_user.id}\nSumma: {money(amount)}",
            inline(("✅ Tasdiqlash", f"depok:{dep_id}"), ("❌ Rad etish", f"depno:{dep_id}")),
            message.photo[-1].file_id
        )
        await message.answer("Chek adminga yuborildi. Tekshiruv kutilmoqda.", reply_markup=menu("buyer"))

    async def deposit_admin(self, callback: CallbackQuery):
        if not self.admin(callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        action, raw = callback.data.split(":")
        dep = await self.db.resolve_deposit(int(raw), action == "depok")
        if not dep:
            await callback.answer("So'rov oldin ko'rilgan.", show_alert=True)
            return
        await callback.answer("Bajarildi")
        if callback.message:
            await callback.message.edit_caption((callback.message.caption or "") + "\n\nKo'rib chiqildi.")
        await self.send_user(dep["user_id"], f"Balans to'ldirish so'rovingiz {'tasdiqlandi' if action == 'depok' else 'rad etildi'}.")

    async def withdraw_start(self, callback: CallbackQuery, state: FSMContext):
        await state.set_state(Withdraw.amount)
        await callback.message.answer("Yechish summasini kiriting:", reply_markup=reply_kb(CANCEL))
        await callback.answer()

    async def withdraw_amount(self, message: Message, state: FSMContext):
        amount = valid_amount(message.text or "", self.settings.min_amount)
        if amount is None:
            await message.answer(f"Eng kam summa {money(self.settings.min_amount)}")
            return
        await state.update_data(amount=amount)
        await state.set_state(Withdraw.card)
        await message.answer("16 xonali karta raqamini kiriting.")

    async def withdraw_card(self, message: Message, state: FSMContext):
        card = (message.text or "").replace(" ", "")
        if not card.isdigit() or len(card) != 16:
            await message.answer("Karta raqami 16 ta raqam bo'lishi kerak.")
            return
        amount = (await state.get_data())["amount"]
        wd_id = await self.db.request_withdrawal(message.from_user.id, amount, card)
        await state.clear()
        if wd_id is None:
            await message.answer("Balans yetarli emas.", reply_markup=menu("buyer"))
            return
        await self.notify_admins(
            f"💸 Yechish #{wd_id}\nFoydalanuvchi: {message.from_user.id}\nSumma: {money(amount)}\nKarta: <code>{esc(card)}</code>",
            inline(("✅ To'landi", f"wdok:{wd_id}"), ("❌ Rad etish", f"wdno:{wd_id}"))
        )
        await message.answer("So'rov adminga yuborildi.", reply_markup=menu("buyer"))

    async def withdraw_admin(self, callback: CallbackQuery):
        if not self.admin(callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        action, raw = callback.data.split(":")
        wd = await self.db.resolve_withdrawal(int(raw), action == "wdok")
        if not wd:
            await callback.answer("So'rov oldin ko'rilgan.", show_alert=True)
            return
        await callback.answer("Bajarildi")
        if callback.message:
            await callback.message.edit_text((callback.message.text or "") + "\n\nKo'rib chiqildi.")
        await self.send_user(wd["user_id"], f"Yechish so'rovingiz {'to\'landi' if action == 'wdok' else 'rad etildi, pul balansga qaytdi'}.")

    async def deals(self, message: Message, state: FSMContext):
        await state.clear()
        rows = await self.db.user_deals(message.from_user.id)
        if not rows:
            await message.answer("Hozircha savdolaringiz yo'q.")
            return
        for d in rows:
            seller = d["seller_id"] == message.from_user.id
            label = "Sotuvchi" if seller else "Xaridor"
            text = f"#{d['id']} — {label} — {money(d['price'])} — <b>{d['status']}</b>"
            markup = None
            if seller and d["status"] in ("listed", "joined"):
                text += f"\nKod: <code>{d['code']}</code>"
                markup = inline(("🗑 E'lonni bekor qilish", f"cancel:{d['id']}"))
            await message.answer(text, reply_markup=markup)

    async def cancel_deal(self, callback: CallbackQuery):
        try:
            deal = await self.db.cancel_deal(int(callback.data.split(":")[1]), callback.from_user.id)
        except (ValueError, IndexError):
            deal = None
        if not deal:
            await callback.answer("Bekor qilib bo'lmaydi.", show_alert=True)
            return
        await callback.answer("Bekor qilindi")
        if callback.message:
            await callback.message.edit_text(f"#{deal['id']} e'lon bekor qilindi.")
        if deal["buyer_id"]:
            await self.send_user(deal["buyer_id"], f"#{deal['id']} e'lon sotuvchi tomonidan bekor qilindi.")

    async def stats(self, message: Message):
        if not self.admin(message.from_user.id):
            return
        row = await self.db.stats()
        await message.answer(f"Yakunlangan savdolar: {row['n']}\nKomissiya daromadi: {money(row['fees'])}")

    async def expired_watcher(self):
        while True:
            try:
                for deal_id in await self.db.expired_deal_ids():
                    deal = await self.db.mark_dispute_by_timeout(deal_id) if hasattr(self.db, "mark_dispute_by_timeout") else None
                    if deal:
                        await self.notify_admins(
                            f"⏰ #{deal_id} tekshirish vaqti tugadi. Admin qarori kerak.",
                            inline(("💰 Sotuvchiga o'tkazish", f"resolve_release:{deal_id}"),
                                   ("↩️ Xaridorga qaytarish", f"resolve_refund:{deal_id}"))
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Muddat tekshirish xatosi")
            await asyncio.sleep(20)


def setup_handlers(bot: Bot, db: Database, settings: Settings):
    app = App(bot, db, settings)

    @router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def start(message: Message, state: FSMContext): await app.start(message, state)

    @router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
    @router.message(F.text == CANCEL, F.chat.type == ChatType.PRIVATE)
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Amal bekor qilindi.", reply_markup=menu((await db.get_user(message.from_user.id) or {}).get("role")))

    @router.callback_query(F.data == "agree")
    async def agree(c: CallbackQuery): await app.agree(c)

    @router.callback_query(F.data == "disagree")
    async def disagree(c: CallbackQuery, state: FSMContext): await app.disagree(c, state)

    @router.callback_query(F.data.startswith("role:"))
    async def role(c: CallbackQuery): await app.choose_role(c)

    @router.message(F.text == "🔄 Rolni almashtirish", F.chat.type == ChatType.PRIVATE)
    async def change_role(m: Message):
        await m.answer("Yangi rolni tanlang:", reply_markup=inline(("💰 Sotuvchi", "role:seller"), ("🛒 Xaridor", "role:buyer")))

    @router.message(F.text == SELL, F.chat.type == ChatType.PRIVATE)
    async def sell(m: Message, s: FSMContext): await app.sell_start(m, s)
    @router.message(Sell.desc, F.text)
    async def sell_desc(m: Message, s: FSMContext): await app.sell_desc(m, s)
    @router.message(Sell.photos, F.photo)
    async def sell_photo(m: Message, s: FSMContext): await app.sell_photo(m, s)
    @router.message(Sell.photos, F.text == DONE)
    async def sell_done(m: Message, s: FSMContext): await app.sell_photos_done(m, s)
    @router.message(Sell.price, F.text)
    async def sell_price(m: Message, s: FSMContext): await app.sell_price(m, s)
    @router.message(Sell.phone, F.text)
    async def sell_phone(m: Message, s: FSMContext): await app.sell_phone(m, s)
    @router.message(Sell.transfer, F.text)
    async def sell_transfer(m: Message, s: FSMContext): await app.sell_transfer(m, s)

    @router.message(F.text == BUY, F.chat.type == ChatType.PRIVATE)
    async def buy(m: Message, s: FSMContext): await app.buy_start(m, s)
    @router.message(Buy.code, F.text)
    async def buy_code(m: Message, s: FSMContext): await app.buy_code(m, s)
    @router.message(Buy.phone, F.text)
    async def buy_phone(m: Message, s: FSMContext): await app.buy_phone(m, s)

    @router.callback_query(F.data.startswith("pay:"))
    async def pay(c: CallbackQuery): await app.pay(c)
    @router.callback_query(F.data.regexp(r"^(ok|dispute):\d+$"))
    async def decision(c: CallbackQuery): await app.buyer_decision(c)
    @router.callback_query(F.data.regexp(r"^resolve_(release|refund):\d+$"))
    async def resolve(c: CallbackQuery): await app.resolve_dispute(c)

    @router.message(F.text == BAL, F.chat.type == ChatType.PRIVATE)
    async def balance(m: Message, s: FSMContext): await app.balance(m, s)
    @router.callback_query(F.data == "dep")
    async def dep(c: CallbackQuery, s: FSMContext): await app.deposit_start(c, s)
    @router.message(Deposit.amount, F.text)
    async def dep_amount(m: Message, s: FSMContext): await app.deposit_amount(m, s)
    @router.message(Deposit.photo, F.photo)
    async def dep_photo(m: Message, s: FSMContext): await app.deposit_photo(m, s)
    @router.callback_query(F.data.regexp(r"^dep(ok|no):\d+$"))
    async def dep_admin(c: CallbackQuery): await app.deposit_admin(c)
    @router.callback_query(F.data == "wd")
    async def wd(c: CallbackQuery, s: FSMContext): await app.withdraw_start(c, s)
    @router.message(Withdraw.amount, F.text)
    async def wd_amount(m: Message, s: FSMContext): await app.withdraw_amount(m, s)
    @router.message(Withdraw.card, F.text)
    async def wd_card(m: Message, s: FSMContext): await app.withdraw_card(m, s)
    @router.callback_query(F.data.regexp(r"^wd(ok|no):\d+$"))
    async def wd_admin(c: CallbackQuery): await app.withdraw_admin(c)

    @router.message(F.text == DEALS, F.chat.type == ChatType.PRIVATE)
    async def deals(m: Message, s: FSMContext): await app.deals(m, s)
    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_deal(c: CallbackQuery): await app.cancel_deal(c)
    @router.message(Command("stats"))
    async def stats(m: Message): await app.stats(m)

    return app
'''

files["main.py"] = r'''
import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import ConfigError, load_settings, setup_logging
from database import Database
from handlers import setup_handlers

log = logging.getLogger(__name__)


async def health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        pass
    body = b"OK"
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\n" + body
    )
    try:
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def main():
    settings = load_settings()
    setup_logging()

    db = Database(settings.database_url)
    await db.connect()
    await db.init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    app = setup_handlers(bot, db, settings)
    dp.include_router(__import__("handlers").router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="cancel", description="Amalni bekor qilish"),
        BotCommand(command="stats", description="Admin statistikasi"),
    ])

    server = None
    port = os.getenv("PORT", "").strip()
    if port:
        server = await asyncio.start_server(health_handler, "0.0.0.0", int(port))
        log.info("Health server %s portda ishga tushdi", port)

    watcher = asyncio.create_task(app.expired_watcher())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("PUBG Garant Bot ishga tushdi")
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if server:
            server.close()
            await server.wait_closed()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"Sozlama xatosi: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Bot to'xtatildi")
'''

# Add timeout-to-dispute database method before stats method
files["database.py"] = files["database.py"].replace(
'''    async def stats(self) -> dict[str, int]:''',
'''    async def mark_dispute_by_timeout(self, deal_id: int):
        row = await self._pool().fetchrow(
            """UPDATE deals SET status='dispute', expires_at=NULL
               WHERE id=$1 AND status='paid' AND expires_at <= NOW() RETURNING *""",
            deal_id,
        )
        return dict(row) if row else None

    async def stats(self) -> dict[str, int]:'''
)

files["requirements.txt"] = """aiogram>=3.13,<4.0
asyncpg>=0.29,<1.0
python-dotenv>=1.0,<2.0
"""

files[".env.example"] = """BOT_TOKEN=PASTE_TELEGRAM_BOT_TOKEN
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
ADMIN_IDS=123456789
FEE_PERCENT=5
MIN_AMOUNT=10000
REVIEW_MINUTES=30
ADMIN_CARD=8600 0000 0000 0000
LOG_LEVEL=INFO
"""

files[".gitignore"] = """.env
venv/
.venv/
__pycache__/
*.py[cod]
*.log
.idea/
.vscode/
"""

files["README.md"] = r'''
# PUBG Garant Bot

Aiogram 3 + PostgreSQL asosidagi Telegram akkaunt savdosi uchun boshlang'ich loyiha.

## Fayllar

- `main.py` — botni ishga tushiradi, Render health-check serverini ochadi.
- `handlers.py` — rozilik, rol tanlash, e'lon, xarid, balans, admin va nizo jarayonlari.
- `database.py` — PostgreSQL jadvallari va tranzaksiyalar.
- `config.py` — `.env` sozlamalarini tekshiradi.
- `.env.example` — kerakli environment o'zgaruvchilari namunasi.

## Muhim xavfsizlik

1. Bot shaxsiy Gmail, Telegram yoki bank parollarini so'ramaydi.
2. Sotuvchi faqat PUBG akkauntini topshirish uchun zarur bo'lgan ma'lumotlarni yuborishi kerak.
3. Xaridor shunchaki fikridan qaytsa pul qaytarilmaydi.
4. Akkaunt topshirilmagani yoki e'lon tavsifiga mos emasligi bo'yicha nizo ochilsa, pul admin qarorigacha ushlab turiladi.
5. Admin dalillarni tekshirib, pulni sotuvchiga o'tkazadi yoki xaridorga qaytaradi.
6. Bu kod avtomatik to'lov provayderiga ulanmagan: balans to'ldirish chekini admin qo'lda tekshiradi.
7. Ishga tushirishdan oldin amaldagi qonunlar va PUBG/KRAFTON xizmat shartlarini tekshiring. O'yin akkauntlarini sotish o'yin qoidalariga zid bo'lishi mumkin.

## Windows kompyuterda ishga tushirish

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

`.env.example` faylidan nusxa olib `.env` nomlang va qiymatlarni kiriting.

```bash
python main.py
```

## Render sozlamalari

1. GitHub repository yarating va loyiha fayllarini yuklang.
2. Render'da PostgreSQL database yarating.
3. Render Web Service yarating va repository'ni ulang.
4. Build Command:

```bash
pip install -r requirements.txt
```

5. Start Command:

```bash
python main.py
```

6. Environment bo'limida quyidagilarni kiriting:
   - `BOT_TOKEN`
   - `DATABASE_URL` — Render PostgreSQL Internal Database URL
   - `ADMIN_IDS` — admin Telegram ID (bir nechta bo'lsa vergul bilan)
   - `FEE_PERCENT`
   - `MIN_AMOUNT`
   - `REVIEW_MINUTES`
   - `ADMIN_CARD`

Render'dagi PostgreSQL va Web Service bir regionda bo'lsa, Internal Database URL ishlating.
Bot polling rejimida ishlaydi. Bitta Web Service instance ishlating.

## Admin

- `/stats` — yakunlangan savdolar va komissiya statistikasi.
- Depozit va yechish so'rovlari adminlarga inline tugmalar bilan yuboriladi.
- Xaridor nizo ochsa, adminlarga ikki qaror tugmasi yuboriladi.

## Eslatma

Bu loyiha real pul bilan ishga tushirilishidan oldin test muhitida sinovdan o'tkazilishi kerak. Admin identifikatori va to'lov rekvizitlarini to'g'ri kiriting. Telegram bot tokeni va DATABASE_URL ni hech qachon GitHub'ga joylamang.
'''

for name, content in files.items():
    path = os.path.join(base, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(textwrap.dedent(content).lstrip())

# Syntax-check all Python source files
for name in ("config.py", "database.py", "handlers.py", "main.py"):
    py_compile.compile(os.path.join(base, name), doraise=True)

zip_path = "/mnt/data/PUBG-Garant-Bot.zip"
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for name in files:
        z.write(os.path.join(base, name), arcname=f"PUBG-Garant-Bot/{name}")

print("Loyiha yaratildi:", base)
print("ZIP:", zip_path)
print("Python syntax tekshiruvi: OK")
print("Fayllar:", ", ".join(files.keys()))
