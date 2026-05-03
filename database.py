import re
from datetime import datetime

import aiosqlite

DB_PATH = "cityclinic_feedback.db"


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                patient_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                phone_normalized TEXT,
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                umnico_lead_id INTEGER,
                umnico_custom_id TEXT,
                rating INTEGER,
                review_text TEXT,
                status TEXT NOT NULL DEFAULT 'started',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES feedback_sessions(id)
            )
        """)
        await db.execute("""
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
        """)

        # Мягкая миграция для старых баз Railway, если таблица уже была создана раньше.
        await _add_column_if_missing(db, "feedback_sessions", "phone_normalized", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "channel", "TEXT NOT NULL DEFAULT 'whatsapp'")
        await _add_column_if_missing(db, "feedback_sessions", "umnico_lead_id", "INTEGER")
        await _add_column_if_missing(db, "feedback_sessions", "umnico_custom_id", "TEXT")
        await db.commit()


async def _add_column_if_missing(db: aiosqlite.Connection, table: str, column: str, column_type: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in await cursor.fetchall()]
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


async def create_session(user_id: int, patient_name: str, phone: str, channel: str = "whatsapp") -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO feedback_sessions
            (user_id, patient_name, phone, phone_normalized, channel, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, patient_name, phone, normalize_phone(phone), channel, "waiting_rating", now, now))
        await db.commit()
        return int(cursor.lastrowid)


async def has_patient_already_reviewed(phone: str) -> bool:
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM reviewed_patients WHERE phone_normalized = ? LIMIT 1",
            (phone_normalized,),
        )
        return await cursor.fetchone() is not None


async def add_reviewed_patient(phone: str, patient_name: str | None = None, session_id: int | None = None,
                               review_text: str | None = None, rating: int | None = None) -> None:
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO reviewed_patients
            (phone_normalized, original_phone, patient_name, session_id, review_text, rating, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (phone_normalized, phone, patient_name, session_id, review_text, rating, datetime.utcnow().isoformat()))
        await db.commit()


async def get_all_reviewed_phones() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT original_phone, phone_normalized, patient_name, rating, created_at
            FROM reviewed_patients
            ORDER BY id DESC
        """)
        return [dict(row) for row in await cursor.fetchall()]


async def delete_reviewed_phone(phone: str) -> int:
    phone_normalized = normalize_phone(phone)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM reviewed_patients WHERE phone_normalized = ?", (phone_normalized,))
        await db.commit()
        return cursor.rowcount


async def get_latest_session(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM feedback_sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_session_by_id(session_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feedback_sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_session_by_lead_id(lead_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feedback_sessions WHERE umnico_lead_id = ? ORDER BY id DESC LIMIT 1", (lead_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_session_by_custom_id(custom_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feedback_sessions WHERE umnico_custom_id = ? ORDER BY id DESC LIMIT 1", (custom_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_session_by_phone(phone: str) -> dict | None:
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM feedback_sessions
            WHERE phone_normalized = ? AND status IN ('waiting_rating', 'waiting_positive_review', 'waiting_negative_feedback')
            ORDER BY id DESC LIMIT 1
        """, (phone_normalized,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_session(session_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    columns = ", ".join(f"{key} = ?" for key in fields.keys())
    values = list(fields.values()) + [session_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE feedback_sessions SET {columns} WHERE id = ?", values)
        await db.commit()


async def add_message_log(session_id: int, sender: str, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO messages_log (session_id, sender, text, created_at)
            VALUES (?, ?, ?, ?)
        """, (session_id, sender, text, datetime.utcnow().isoformat()))
        await db.commit()


async def get_session_messages(session_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT sender, text, created_at
            FROM messages_log
            WHERE session_id = ?
            ORDER BY id ASC
        """, (session_id,))
        return [dict(row) for row in await cursor.fetchall()]
