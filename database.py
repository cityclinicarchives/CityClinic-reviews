import re
from datetime import datetime

import aiosqlite

DB_PATH = "cityclinic_feedback.db"


def normalize_phone(phone: str) -> str:
    """
    Приводит телефон к единому виду для надежной проверки дублей.

    Примеры:
    +7 (999) 123-45-67 -> 79991234567
    8 999 123 45 67     -> 79991234567
    """
    digits = re.sub(r"\D", "", phone or "")

    # Российские номера часто пишут через 8 вместо +7.
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    return digits


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                patient_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                rating INTEGER,
                review_text TEXT,
                status TEXT NOT NULL DEFAULT 'started',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES feedback_sessions(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewed_patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_normalized TEXT NOT NULL UNIQUE,
                original_phone TEXT NOT NULL,
                patient_name TEXT,
                session_id INTEGER,
                review_text TEXT,
                rating INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES feedback_sessions(id)
            )
            """
        )
        await db.commit()


async def create_session(user_id: int, patient_name: str, phone: str) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO feedback_sessions
            (user_id, patient_name, phone, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, patient_name, phone, "waiting_rating", now, now),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def has_patient_already_reviewed(phone: str) -> bool:
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT 1
            FROM reviewed_patients
            WHERE phone_normalized = ?
            LIMIT 1
            """,
            (phone_normalized,),
        )
        row = await cursor.fetchone()
        return row is not None


async def add_reviewed_patient(
    phone: str,
    patient_name: str | None = None,
    session_id: int | None = None,
    review_text: str | None = None,
    rating: int | None = None,
) -> None:
    """
    Записывает телефон пациента в базу уже оставивших отзыв.
    INSERT OR IGNORE защищает от ошибки, если номер уже был добавлен ранее.
    """
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO reviewed_patients
            (phone_normalized, original_phone, patient_name, session_id, review_text, rating, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                phone_normalized,
                phone,
                patient_name,
                session_id,
                review_text,
                rating,
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def get_latest_session(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM feedback_sessions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_session(session_id: int, **fields) -> None:
    if not fields:
        return

    fields["updated_at"] = datetime.utcnow().isoformat()
    columns = ", ".join(f"{key} = ?" for key in fields.keys())
    values = list(fields.values()) + [session_id]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE feedback_sessions SET {columns} WHERE id = ?",
            values,
        )
        await db.commit()


async def add_message_log(session_id: int, sender: str, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages_log (session_id, sender, text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, sender, text, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_session_messages(session_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT sender, text, created_at
            FROM messages_log
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_all_reviewed_patients() -> list[dict]:
    """Возвращает список пациентов, уже оставивших отзыв."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT phone_normalized, original_phone, patient_name, rating, created_at
            FROM reviewed_patients
            ORDER BY id DESC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_reviewed_patient(phone: str) -> bool:
    """Удаляет телефон из базы уже оставивших отзывов. Возвращает True, если номер был удален."""
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM reviewed_patients WHERE phone_normalized = ?",
            (phone_normalized,),
        )
        await db.commit()
        return cursor.rowcount > 0
