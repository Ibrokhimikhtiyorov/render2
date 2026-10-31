from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from config import BASE_DIR
from database import Database

CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
REQUIRED_FIELDS = (
    "code",
    "grade",
    "text",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
    "explanation",
    "source",
)


def load_questions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Savollar fayli topilmadi: {path}")
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, list) or not data:
        raise ValueError("questions.json ildizida bo‘sh bo‘lmagan ro‘yxat bo‘lishi kerak.")
    return data


def validate_questions(items: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    seen_codes: set[str] = set()
    for index, item in enumerate(items, start=1):
        where = f"{index}-yozuv"
        if not isinstance(item, dict):
            errors.append(f"{where}: obyekt emas.")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in item]
        if missing:
            errors.append(f"{where}: yetishmayotgan maydonlar: {', '.join(missing)}")
            continue
        code = str(item["code"]).strip().lower()
        if not CODE_RE.fullmatch(code):
            errors.append(f"{where}: code noto‘g‘ri ({code!r}).")
        if code in seen_codes:
            errors.append(f"{where}: code takrorlangan ({code}).")
        seen_codes.add(code)
        item["code"] = code

        try:
            grade = int(item["grade"])
        except (TypeError, ValueError):
            errors.append(f"{where}: grade butun son emas.")
            grade = 0
        if not 1 <= grade <= 11:
            errors.append(f"{where}: grade 1–11 oralig‘ida bo‘lishi kerak.")
        item["grade"] = grade

        for field in ("text", "explanation", "source"):
            value = str(item[field]).strip()
            if len(value) < 3:
                errors.append(f"{where}: {field} juda qisqa.")
            item[field] = value

        options: list[str] = []
        for field in ("option_a", "option_b", "option_c", "option_d"):
            value = str(item[field]).strip()
            if not value:
                errors.append(f"{where}: {field} bo‘sh.")
            options.append(value.casefold())
            item[field] = value
        if len(set(options)) != 4:
            errors.append(f"{where}: variantlar takrorlanmasligi kerak.")

        try:
            correct = int(item["correct_option"])
        except (TypeError, ValueError):
            errors.append(f"{where}: correct_option butun son emas.")
            correct = -1
        if not 0 <= correct <= 3:
            errors.append(f"{where}: correct_option 0–3 oralig‘ida bo‘lishi kerak.")
        item["correct_option"] = correct

        image = item.get("image_file_id")
        if image is not None:
            image = str(image).strip() or None
        item["image_file_id"] = image
        item["is_active"] = bool(item.get("is_active", True))

    if errors:
        preview = "\n".join(f" - {error}" for error in errors[:30])
        raise ValueError(f"Savollarda {len(errors)} ta xato bor:\n{preview}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Huquq quiz savollarini SQLite bazaga import qilish.")
    parser.add_argument("--file", default=str(BASE_DIR / "questions.json"), help="JSON fayl yo‘li")
    parser.add_argument("--db", default=str(BASE_DIR / "huquq_quiz.db"), help="SQLite baza yo‘li")
    parser.add_argument("--dry-run", action="store_true", help="Bazaga yozmasdan tekshirish")
    args = parser.parse_args()

    path = Path(args.file)
    try:
        items = load_questions(path)
        validate_questions(items)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"XATO: {exc}", file=sys.stderr)
        return 1

    counts = Counter(int(item["grade"]) for item in items)
    print(f"Tekshirildi: {len(items)} ta savol")
    for grade in sorted(counts):
        print(f"  {grade}-sinf: {counts[grade]} ta")
    if args.dry_run:
        print("Dry-run: baza o‘zgartirilmadi.")
        return 0

    db = Database(args.db)
    db.init_db()
    added, updated = db.seed_questions(items)
    print(f"Baza: {db.path}")
    print(f"Qo‘shildi: {added}, yangilandi: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
