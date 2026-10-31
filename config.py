from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


class ConfigError(RuntimeError):
    pass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} butun son bo'lishi kerak, hozir: {raw!r}") from exc


def _parse_ids(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS ichida noto'g'ri ID bor: {part!r}") from exc
    return frozenset(result)


def _parse_grades(raw: str) -> tuple[int, ...]:
    grades: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            grade = int(part)
        except ValueError as exc:
            raise ConfigError(f"QUIZ_GRADES ichida noto'g'ri sinf bor: {part!r}") from exc
        if not 1 <= grade <= 11:
            raise ConfigError(f"Sinf 1 dan 11 gacha bo'lishi kerak: {grade}")
        if grade not in grades:
            grades.append(grade)
    if not grades:
        raise ConfigError("QUIZ_GRADES bo'sh bo'lishi mumkin emas.")
    return tuple(grades)


def _parse_optional_chat_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw or raw == "0":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError("GROUP_CHAT_ID butun son bo'lishi kerak.") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    group_chat_id: int | None
    fixed_grade: int | None
    quiz_grades: tuple[int, ...]
    questions_per_quiz: int
    question_seconds: int
    announce_hour: int
    announce_minute: int
    quiz_hour: int
    quiz_minute: int
    recent_question_days: int
    timezone: ZoneInfo
    db_path: Path
    questions_file: Path
    log_file: Path

    def grade_for_date(self, day: date) -> int:
        if self.fixed_grade is not None:
            return self.fixed_grade
        return self.quiz_grades[day.toordinal() % len(self.quiz_grades)]

    def next_grade(self, day: date) -> int:
        if self.fixed_grade is not None:
            return self.fixed_grade
        index = (day.toordinal() + 1) % len(self.quiz_grades)
        return self.quiz_grades[index]

    def today(self) -> date:
        return datetime.now(self.timezone).date()


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise ConfigError(
            "BOT_TOKEN topilmadi. .env.example faylidan .env yaratib, "
            "BotFather bergan tokenni BOT_TOKEN ga yozing."
        )
    if token in {"YOUR_BOT_TOKEN", "123456:ABC-DEF", "PASTE_YOUR_TOKEN_HERE"}:
        raise ConfigError("BOT_TOKEN hali namuna qiymatda. BotFather bergan haqiqiy tokenni kiriting.")

    admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""))
    group_chat_id = _parse_optional_chat_id(os.getenv("GROUP_CHAT_ID", ""))

    raw_grade = os.getenv("QUIZ_GRADE", "0").strip() or "0"
    try:
        grade_number = int(raw_grade)
    except ValueError as exc:
        raise ConfigError("QUIZ_GRADE butun son bo'lishi kerak (0 — sinflar almashadi).") from exc

    quiz_grades = _parse_grades(os.getenv("QUIZ_GRADES", "8,9,10,11"))
    if grade_number == 0:
        fixed_grade = None
    else:
        if not 1 <= grade_number <= 11:
            raise ConfigError("QUIZ_GRADE 0 yoki 1–11 oralig'ida bo'lishi kerak.")
        fixed_grade = grade_number

    questions_per_quiz = _env_int("QUESTIONS_PER_QUIZ", 5)
    if not 1 <= questions_per_quiz <= 30:
        raise ConfigError("QUESTIONS_PER_QUIZ 1 dan 30 gacha bo'lishi kerak.")

    question_seconds = _env_int("QUESTION_SECONDS", 25)
    if not 20 <= question_seconds <= 30:
        raise ConfigError("QUESTION_SECONDS 20 dan 30 gacha bo'lishi kerak.")

    announce_hour = _env_int("ANNOUNCE_HOUR", 20)
    announce_minute = _env_int("ANNOUNCE_MINUTE", 0)
    quiz_hour = _env_int("QUIZ_HOUR", 20)
    quiz_minute = _env_int("QUIZ_MINUTE", 5)
    if not 0 <= announce_hour <= 23 or not 0 <= quiz_hour <= 23:
        raise ConfigError("Soat 0 dan 23 gacha bo'lishi kerak.")
    if not 0 <= announce_minute <= 59 or not 0 <= quiz_minute <= 59:
        raise ConfigError("Daqiqa 0 dan 59 gacha bo'lishi kerak.")

    recent_days = _env_int("RECENT_QUESTION_DAYS", 30)
    if not 0 <= recent_days <= 365:
        raise ConfigError("RECENT_QUESTION_DAYS 0 dan 365 gacha bo'lishi kerak.")

    timezone_name = os.getenv("TIMEZONE", "Asia/Tashkent").strip() or "Asia/Tashkent"
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ConfigError(
            f"Vaqt zonasi topilmadi: {timezone_name}. Windowsda tzdata paketi o'rnatilganini tekshiring."
        ) from exc

    db_path = Path(os.getenv("DB_PATH", "huquq_quiz.db").strip() or "huquq_quiz.db")
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    questions_file = Path(os.getenv("QUESTIONS_FILE", "questions.json").strip() or "questions.json")
    if not questions_file.is_absolute():
        questions_file = BASE_DIR / questions_file

    log_file = Path(os.getenv("LOG_FILE", "logs/bot.log").strip() or "logs/bot.log")
    if not log_file.is_absolute():
        log_file = BASE_DIR / log_file

    return Settings(
        bot_token=token,
        admin_ids=admin_ids,
        group_chat_id=group_chat_id,
        fixed_grade=fixed_grade,
        quiz_grades=quiz_grades,
        questions_per_quiz=questions_per_quiz,
        question_seconds=question_seconds,
        announce_hour=announce_hour,
        announce_minute=announce_minute,
        quiz_hour=quiz_hour,
        quiz_minute=quiz_minute,
        recent_question_days=recent_days,
        timezone=timezone,
        db_path=db_path,
        questions_file=questions_file,
        log_file=log_file,
    )


def setup_logging(settings: Settings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
