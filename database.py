from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

BASE_DIR = Path(__file__).resolve().parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_in_timezone(tz) -> str:
    return datetime.now(tz).date().isoformat()


@dataclass(frozen=True)
class AnswerResult:
    status: str
    is_correct: bool = False


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = BASE_DIR / self.path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS questions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL UNIQUE,
                        grade INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 11),
                        text TEXT NOT NULL,
                        option_a TEXT NOT NULL,
                        option_b TEXT NOT NULL,
                        option_c TEXT NOT NULL,
                        option_d TEXT NOT NULL,
                        correct_option INTEGER NOT NULL CHECK (correct_option BETWEEN 0 AND 3),
                        explanation TEXT NOT NULL,
                        source TEXT NOT NULL,
                        image_file_id TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_questions_grade_active
                        ON questions (grade, is_active);

                    CREATE TABLE IF NOT EXISTS quiz_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        grade INTEGER NOT NULL,
                        quiz_date TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'auto',
                        status TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'finished', 'cancelled', 'interrupted')),
                        total_questions INTEGER NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS session_questions (
                        session_id INTEGER NOT NULL,
                        question_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        message_id INTEGER,
                        is_photo INTEGER NOT NULL DEFAULT 0 CHECK (is_photo IN (0, 1)),
                        opened_at REAL,
                        closes_at REAL,
                        PRIMARY KEY (session_id, question_id),
                        UNIQUE (session_id, position),
                        FOREIGN KEY (session_id) REFERENCES quiz_sessions(id) ON DELETE CASCADE,
                        FOREIGN KEY (question_id) REFERENCES questions(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_session_questions_question
                        ON session_questions (question_id, session_id);

                    CREATE TABLE IF NOT EXISTS answers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        question_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        selected_option INTEGER NOT NULL CHECK (selected_option BETWEEN 0 AND 3),
                        is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                        answered_at TEXT NOT NULL,
                        UNIQUE (session_id, question_id, user_id),
                        FOREIGN KEY (session_id) REFERENCES quiz_sessions(id) ON DELETE CASCADE,
                        FOREIGN KEY (question_id) REFERENCES questions(id)
                    );

                    CREATE TABLE IF NOT EXISTS participants (
                        session_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        display_name TEXT NOT NULL,
                        username TEXT,
                        score INTEGER NOT NULL DEFAULT 0,
                        correct_count INTEGER NOT NULL DEFAULT 0,
                        answered_count INTEGER NOT NULL DEFAULT 0,
                        joined_at TEXT NOT NULL,
                        last_answer_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, user_id),
                        FOREIGN KEY (session_id) REFERENCES quiz_sessions(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        username TEXT,
                        total_score INTEGER NOT NULL DEFAULT 0,
                        quizzes_played INTEGER NOT NULL DEFAULT 0,
                        answered_questions INTEGER NOT NULL DEFAULT 0,
                        correct_answers INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL
                    );
                    """
                )
            finally:
                conn.close()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else default
            finally:
                conn.close()

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, str(value)),
                )
            finally:
                conn.close()

    def seed_questions(self, questions: Sequence[dict[str, Any]]) -> tuple[int, int]:
        added = 0
        updated = 0
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for item in questions:
                    code = str(item["code"])
                    exists = conn.execute(
                        "SELECT 1 FROM questions WHERE code = ?", (code,)
                    ).fetchone()
                    image = item.get("image_file_id") or None
                    conn.execute(
                        """
                        INSERT INTO questions (
                            code, grade, text, option_a, option_b, option_c, option_d,
                            correct_option, explanation, source, image_file_id,
                            is_active, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET
                            grade = excluded.grade,
                            text = excluded.text,
                            option_a = excluded.option_a,
                            option_b = excluded.option_b,
                            option_c = excluded.option_c,
                            option_d = excluded.option_d,
                            correct_option = excluded.correct_option,
                            explanation = excluded.explanation,
                            source = excluded.source,
                            image_file_id = COALESCE(excluded.image_file_id, questions.image_file_id),
                            is_active = excluded.is_active,
                            updated_at = excluded.updated_at
                        """,
                        (
                            code,
                            int(item["grade"]),
                            str(item["text"]).strip(),
                            str(item["option_a"]).strip(),
                            str(item["option_b"]).strip(),
                            str(item["option_c"]).strip(),
                            str(item["option_d"]).strip(),
                            int(item["correct_option"]),
                            str(item["explanation"]).strip(),
                            str(item["source"]).strip(),
                            image,
                            1 if item.get("is_active", True) else 0,
                            now,
                            now,
                        ),
                    )
                    if exists:
                        updated += 1
                    else:
                        added += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        return added, updated

    def add_question(
        self,
        grade: int,
        text: str,
        options: Sequence[str],
        correct_option: int,
        explanation: str,
        source: str,
        image_file_id: str | None = None,
        code: str | None = None,
    ) -> int:
        if len(options) != 4:
            raise ValueError("Savolda aynan 4 ta variant bo'lishi kerak.")
        if not 0 <= correct_option <= 3:
            raise ValueError("To'g'ri javob 0–3 oralig'ida bo'lishi kerak.")
        now = utc_now()
        code = code or f"manual-{int(time.time() * 1000)}"
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO questions (
                        code, grade, text, option_a, option_b, option_c, option_d,
                        correct_option, explanation, source, image_file_id,
                        is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        code,
                        grade,
                        text.strip(),
                        options[0].strip(),
                        options[1].strip(),
                        options[2].strip(),
                        options[3].strip(),
                        correct_option,
                        explanation.strip(),
                        source.strip(),
                        image_file_id,
                        now,
                        now,
                    ),
                )
                return int(cursor.lastrowid)
            finally:
                conn.close()

    def question_counts(self) -> dict[int, int]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT grade, COUNT(*) AS cnt FROM questions WHERE is_active = 1 GROUP BY grade"
                ).fetchall()
                return {int(row["grade"]): int(row["cnt"]) for row in rows}
            finally:
                conn.close()

    def total_active_questions(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM questions WHERE is_active = 1"
                ).fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()

    def pick_questions(self, grade: int, limit: int, recent_days: int = 30) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT q.* FROM questions AS q
                    WHERE q.grade = ? AND q.is_active = 1
                      AND q.id NOT IN (
                          SELECT sq.question_id
                          FROM session_questions AS sq
                          JOIN quiz_sessions AS s ON s.id = sq.session_id
                          WHERE s.started_at >= ?
                      )
                    ORDER BY RANDOM()
                    LIMIT ?
                    """,
                    (grade, cutoff, limit),
                ).fetchall()
                if len(rows) < limit:
                    rows = conn.execute(
                        """
                        SELECT * FROM questions
                        WHERE grade = ? AND is_active = 1
                        ORDER BY RANDOM()
                        LIMIT ?
                        """,
                        (grade, limit),
                    ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def get_question(self, question_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def find_question(self, identifier: str) -> dict[str, Any] | None:
        identifier = identifier.strip()
        with self._lock:
            conn = self._connect()
            try:
                row = None
                if identifier.isdigit():
                    row = conn.execute(
                        "SELECT * FROM questions WHERE id = ?", (int(identifier),)
                    ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT * FROM questions WHERE code = ?", (identifier,)
                    ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def set_question_image(self, identifier: str, file_id: str | None) -> bool:
        question = self.find_question(identifier)
        if not question:
            return False
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE questions SET image_file_id = ?, updated_at = ? WHERE id = ?",
                    (file_id, utc_now(), question["id"]),
                )
                return True
            finally:
                conn.close()

    def create_quiz_session(
        self,
        chat_id: int,
        grade: int,
        questions: Sequence[dict[str, Any]],
        source: str = "auto",
        quiz_date: str | None = None,
    ) -> int:
        if not questions:
            raise ValueError("Quiz uchun savollar yo'q.")
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    INSERT INTO quiz_sessions
                        (chat_id, grade, quiz_date, source, status, total_questions, started_at)
                    VALUES (?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        chat_id,
                        grade,
                        quiz_date or datetime.now().astimezone().date().isoformat(),
                        source,
                        len(questions),
                        now,
                    ),
                )
                session_id = int(cursor.lastrowid)
                for position, question in enumerate(questions, start=1):
                    conn.execute(
                        """
                        INSERT INTO session_questions (session_id, question_id, position)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, int(question["id"]), position),
                    )
                conn.execute("COMMIT")
                return session_id
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def open_question(self, session_id: int, question_id: int, seconds: int) -> float:
        opened_at = time.time()
        closes_at = opened_at + seconds
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE session_questions
                    SET opened_at = ?, closes_at = ?
                    WHERE session_id = ? AND question_id = ?
                    """,
                    (opened_at, closes_at, session_id, question_id),
                )
                return closes_at
            finally:
                conn.close()

    def set_question_message(
        self, session_id: int, question_id: int, message_id: int, is_photo: bool
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE session_questions
                    SET message_id = ?, is_photo = ?
                    WHERE session_id = ? AND question_id = ?
                    """,
                    (message_id, 1 if is_photo else 0, session_id, question_id),
                )
            finally:
                conn.close()

    def close_question(self, session_id: int, question_id: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE session_questions SET closes_at = ? WHERE session_id = ? AND question_id = ?",
                    (time.time(), session_id, question_id),
                )
            finally:
                conn.close()

    def record_answer(
        self,
        session_id: int,
        question_id: int,
        user_id: int,
        display_name: str,
        username: str | None,
        selected_option: int,
    ) -> AnswerResult:
        if not 0 <= selected_option <= 3:
            return AnswerResult("invalid")
        now = utc_now()
        now_ts = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                session = conn.execute(
                    "SELECT status FROM quiz_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not session or session["status"] != "running":
                    conn.execute("ROLLBACK")
                    return AnswerResult("closed")

                session_question = conn.execute(
                    """
                    SELECT closes_at FROM session_questions
                    WHERE session_id = ? AND question_id = ?
                    """,
                    (session_id, question_id),
                ).fetchone()
                if not session_question or session_question["closes_at"] is None:
                    conn.execute("ROLLBACK")
                    return AnswerResult("closed")
                if now_ts > float(session_question["closes_at"]):
                    conn.execute("ROLLBACK")
                    return AnswerResult("expired")

                question = conn.execute(
                    "SELECT correct_option FROM questions WHERE id = ?", (question_id,)
                ).fetchone()
                if not question:
                    conn.execute("ROLLBACK")
                    return AnswerResult("closed")

                is_correct = int(selected_option) == int(question["correct_option"])
                try:
                    conn.execute(
                        """
                        INSERT INTO answers
                            (session_id, question_id, user_id, selected_option, is_correct, answered_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            question_id,
                            user_id,
                            int(selected_option),
                            1 if is_correct else 0,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
                    return AnswerResult("duplicate")

                participant = conn.execute(
                    "SELECT 1 FROM participants WHERE session_id = ? AND user_id = ?",
                    (session_id, user_id),
                ).fetchone()
                is_new_participant = participant is None

                conn.execute(
                    """
                    INSERT INTO participants (
                        session_id, user_id, display_name, username, score,
                        correct_count, answered_count, joined_at, last_answer_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(session_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        username = excluded.username,
                        score = participants.score + excluded.score,
                        correct_count = participants.correct_count + excluded.correct_count,
                        answered_count = participants.answered_count + 1,
                        last_answer_at = excluded.last_answer_at
                    """,
                    (
                        session_id,
                        user_id,
                        display_name,
                        username,
                        int(is_correct),
                        int(is_correct),
                        now,
                        now,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, display_name, username, total_score, quizzes_played,
                        answered_questions, correct_answers, created_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        username = excluded.username,
                        total_score = users.total_score + excluded.total_score,
                        quizzes_played = users.quizzes_played + excluded.quizzes_played,
                        answered_questions = users.answered_questions + 1,
                        correct_answers = users.correct_answers + excluded.correct_answers,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        user_id,
                        display_name,
                        username,
                        int(is_correct),
                        1 if is_new_participant else 0,
                        int(is_correct),
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return AnswerResult("ok", is_correct)
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def question_answer_stats(self, session_id: int, question_id: int) -> tuple[int, int]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(is_correct), 0) AS correct
                    FROM answers
                    WHERE session_id = ? AND question_id = ?
                    """,
                    (session_id, question_id),
                ).fetchone()
                return int(row["total"]), int(row["correct"])
            finally:
                conn.close()

    def finish_session(self, session_id: int, status: str = "finished") -> None:
        if status not in {"finished", "cancelled", "interrupted"}:
            raise ValueError("Noto'g'ri session statusi.")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE quiz_sessions SET status = ?, finished_at = ? WHERE id = ?",
                    (status, utc_now(), session_id),
                )
            finally:
                conn.close()

    def interrupt_unfinished_sessions(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE quiz_sessions
                    SET status = 'interrupted', finished_at = ?
                    WHERE status = 'running'
                    """,
                    (utc_now(),),
                )
                return int(cursor.rowcount)
            finally:
                conn.close()

    def get_participants(self, session_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ?
                    ORDER BY score DESC, correct_count DESC, joined_at ASC
                    LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def participant_count(self, session_id: int) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM participants WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()

    def user_stats(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def top_users(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM users
                    WHERE total_score > 0
                    ORDER BY total_score DESC, correct_answers DESC, last_seen_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM quiz_sessions WHERE id = ?", (session_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
