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
                wazzup_channel_id TEXT,
                rating INTEGER,
                review_text TEXT,
                ai_was_used INTEGER NOT NULL DEFAULT 0,
                ai_tokens_used INTEGER NOT NULL DEFAULT 0,
                yandex_clicked_at TEXT,
                scenario TEXT NOT NULL DEFAULT 'review',
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
        await _add_column_if_missing(db, "feedback_sessions", "phone_normalized", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "channel", "TEXT NOT NULL DEFAULT 'whatsapp'")
        await _add_column_if_missing(db, "feedback_sessions", "wazzup_channel_id", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "ai_was_used", "INTEGER NOT NULL DEFAULT 0")
        await _add_column_if_missing(db, "feedback_sessions", "ai_tokens_used", "INTEGER NOT NULL DEFAULT 0")
        await _add_column_if_missing(db, "feedback_sessions", "yandex_clicked_at", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "scenario", "TEXT NOT NULL DEFAULT 'review'")
        await _add_column_if_missing(db, "feedback_sessions", "last_wazzup_message_id", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "last_wazzup_crm_message_id", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "last_wazzup_status", "TEXT")
        await _add_column_if_missing(db, "feedback_sessions", "last_wazzup_response", "TEXT")
        await db.commit()


async def _add_column_if_missing(db: aiosqlite.Connection, table: str, column: str, column_type: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in await cursor.fetchall()]
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


async def create_session(user_id: int, patient_name: str, phone: str, channel: str = "whatsapp", wazzup_channel_id: str | None = None, scenario: str = "review", initial_status: str = "waiting_rating") -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO feedback_sessions
            (user_id, patient_name, phone, phone_normalized, channel, wazzup_channel_id, scenario, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, patient_name, phone, normalize_phone(phone), channel, wazzup_channel_id, scenario, initial_status, now, now))
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


async def get_session_by_id(session_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feedback_sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_session_by_phone(phone: str, channel_id: str | None = None) -> dict | None:
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return None
    params: list = [phone_normalized]
    where_channel = ""
    if channel_id:
        where_channel = " AND (wazzup_channel_id = ? OR wazzup_channel_id IS NULL)"
        params.append(channel_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"""
            SELECT * FROM feedback_sessions
            WHERE phone_normalized = ?
              AND status IN ('waiting_rating', 'waiting_negative_feedback', 'cashback_waiting_reply')
              {where_channel}
            ORDER BY id DESC LIMIT 1
        """, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_latest_session_by_phone(phone: str, channel_id: str | None = None) -> dict | None:
    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return None
    params: list = [phone_normalized]
    where_channel = ""
    if channel_id:
        where_channel = " AND (wazzup_channel_id = ? OR wazzup_channel_id IS NULL)"
        params.append(channel_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"""
            SELECT * FROM feedback_sessions
            WHERE phone_normalized = ?
              {where_channel}
            ORDER BY id DESC LIMIT 1
        """, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_ai_usage(session_id: int, tokens_used: int) -> None:
    tokens_used = max(0, int(tokens_used or 0))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE feedback_sessions
            SET ai_was_used = 1,
                ai_tokens_used = COALESCE(ai_tokens_used, 0) + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (tokens_used, datetime.utcnow().isoformat(), session_id),
        )
        await db.commit()


async def mark_yandex_clicked(session_id: int) -> tuple[dict | None, bool]:
    """
    Фиксирует первый переход пациента по ссылке на Яндекс.Карты.
    Возвращает: (session, is_first_click).
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feedback_sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        if not row:
            return None, False

        session = dict(row)
        is_first_click = not bool(session.get("yandex_clicked_at"))
        if is_first_click:
            await db.execute(
                "UPDATE feedback_sessions SET yandex_clicked_at = ?, updated_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            await db.commit()
            session["yandex_clicked_at"] = now

        return session, is_first_click


async def update_session(session_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    columns = ", ".join(f"{key} = ?" for key in fields.keys())
    values = list(fields.values()) + [session_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE feedback_sessions SET {columns} WHERE id = ?", values)
        await db.commit()


async def update_wazzup_delivery(
    session_id: int,
    *,
    message_id: str | None = None,
    crm_message_id: str | None = None,
    status: str | None = None,
    response: str | None = None,
) -> None:
    fields = {}
    if message_id:
        fields["last_wazzup_message_id"] = message_id
    if crm_message_id:
        fields["last_wazzup_crm_message_id"] = crm_message_id
    if status:
        fields["last_wazzup_status"] = status
    if response:
        fields["last_wazzup_response"] = response[:3000]
    if fields:
        await update_session(session_id, **fields)


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
