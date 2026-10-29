import os
from pathlib import Path

import aiosqlite

from config import settings

DB_PATH = str(Path(settings.DATA_DIR) / "users.db")


async def init_db():
    Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                account_uid INTEGER NOT NULL,
                phone TEXT NOT NULL,
                first_name TEXT DEFAULT '',
                session_string TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_id, phone)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                target_link TEXT NOT NULL,
                reason TEXT NOT NULL,
                comment TEXT DEFAULT '',
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


async def add_account(owner_id: int, account_uid: int, phone: str,
                      first_name: str, session_string: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO accounts (owner_id, account_uid, phone, first_name, session_string)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, phone) DO UPDATE SET
                    session_string = excluded.session_string,
                    first_name = excluded.first_name,
                    account_uid = excluded.account_uid
            """, (owner_id, account_uid, phone, first_name, session_string))
            await db.commit()
        return True
    except Exception:
        return False


async def get_accounts(owner_id: int) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT phone, first_name, session_string FROM accounts "
            "WHERE owner_id = ? ORDER BY id",
            (owner_id,)
        )
        return [tuple(row) for row in await cur.fetchall()]


async def get_session(owner_id: int, phone: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT session_string FROM accounts WHERE owner_id = ? AND phone = ?",
            (owner_id, phone)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def remove_account(owner_id: int, phone: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM accounts WHERE owner_id = ? AND phone = ?",
            (owner_id, phone)
        )
        await db.commit()
        return cur.rowcount > 0


async def count_accounts(owner_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM accounts WHERE owner_id = ?", (owner_id,)
        )
        return (await cur.fetchone())[0]


async def mark_invalid(owner_id: int, phone: str):
    """Сессия умерла (аккаунт разлогинили) — убираем её."""
    await remove_account(owner_id, phone)


async def save_complaint(owner_id: int, phone: str, link: str,
                         reason: str, comment: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO complaints
            (owner_id, phone, target_link, reason, comment, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (owner_id, phone, link, reason, comment, status))
        await db.commit()


async def get_history(owner_id: int, limit: int = 10) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT phone, target_link, reason, status, created_at
            FROM complaints WHERE owner_id = ?
            ORDER BY id DESC LIMIT ?
        """, (owner_id, limit))
        return await cur.fetchall()
