"""SQLite-сховище: недавні повідомлення, сусіди, що представилися, лічильники дій бота.

Повідомлення зберігаються лише як короткий контекст для модератора
і видаляються через KEEP_MESSAGES_SECONDS.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

KEEP_MESSAGES_SECONDS = 3 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_by INTEGER,
    added_at REAL,
    active INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    last_analyzed_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_to INTEGER,
    ts REAL NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    addressed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE TABLE IF NOT EXISTS residents (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    apartment TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    intro_reminded_at REAL,
    intro_reminder_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS pending_intros (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    greeting_message_id INTEGER,
    muted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS events (
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    user_id INTEGER
);
CREATE INDEX IF NOT EXISTS events_chat_kind_ts ON events (chat_id, kind, ts);
"""


@dataclass(frozen=True)
class Chat:
    chat_id: int
    title: str | None
    added_by: int | None
    active: bool
    paused: bool
    last_analyzed_id: int


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    user_id: int | None
    author: str
    text: str
    reply_to: int | None
    ts: float
    is_bot: bool
    addressed: bool


@dataclass(frozen=True)
class Resident:
    user_id: int
    name: str
    apartment: str


@dataclass(frozen=True)
class PendingIntro:
    """Людина, яка ще має представитися (новачок без права писати або той, хто натиснув «Представитися»)."""

    chat_id: int
    user_id: int
    greeting_message_id: int | None
    muted: bool


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(members)")}
        if "intro_reminder_count" not in columns:
            self.db.execute(
                "ALTER TABLE members ADD COLUMN intro_reminder_count INTEGER NOT NULL DEFAULT 0"
            )
            self.db.execute(
                "UPDATE members SET intro_reminder_count = 1 WHERE intro_reminded_at IS NOT NULL"
            )
        if "user_id" not in {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN user_id INTEGER")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_user_kind_ts ON events (user_id, kind, ts)")

    # --- чати -------------------------------------------------------------

    def upsert_chat(self, chat_id: int, title: str | None, added_by: int | None) -> None:
        self.db.execute(
            """
            INSERT INTO chats (chat_id, title, added_by, added_at, active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT (chat_id) DO UPDATE SET
                title = excluded.title,
                added_by = COALESCE(excluded.added_by, chats.added_by),
                active = 1
            """,
            (chat_id, title, added_by, time.time()),
        )
        self.db.commit()

    def get_chat(self, chat_id: int) -> Chat | None:
        row = self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        return Chat(
            chat_id=row["chat_id"],
            title=row["title"],
            added_by=row["added_by"],
            active=bool(row["active"]),
            paused=bool(row["paused"]),
            last_analyzed_id=row["last_analyzed_id"],
        )

    def list_chats(self) -> list[Chat]:
        ids = [r[0] for r in self.db.execute("SELECT chat_id FROM chats WHERE active = 1")]
        return [chat for chat_id in ids if (chat := self.get_chat(chat_id))]

    def deactivate_chat(self, chat_id: int) -> None:
        self.db.execute("UPDATE chats SET active = 0 WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    def set_paused(self, chat_id: int, paused: bool) -> None:
        self.db.execute("UPDATE chats SET paused = ? WHERE chat_id = ?", (int(paused), chat_id))
        self.db.commit()

    def set_last_analyzed(self, chat_id: int, message_id: int) -> None:
        self.db.execute(
            "UPDATE chats SET last_analyzed_id = MAX(last_analyzed_id, ?) WHERE chat_id = ?",
            (message_id, chat_id),
        )
        self.db.commit()

    def migrate_chat(self, old_id: int, new_id: int) -> None:
        """Група стала супергрупою — Telegram видав їй новий chat_id."""
        # У супергрупи своя нумерація повідомлень, тож старий контекст лише заважатиме.
        self.db.execute("DELETE FROM messages WHERE chat_id = ?", (old_id,))
        for table in ("chats", "residents", "members", "events", "pending_intros"):
            self.db.execute(f"UPDATE OR IGNORE {table} SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
        self.db.execute("UPDATE chats SET last_analyzed_id = 0 WHERE chat_id = ?", (new_id,))
        self.db.commit()

    # --- повідомлення -----------------------------------------------------

    def add_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: int | None,
        author: str,
        text: str,
        reply_to: int | None,
        ts: float,
        *,
        is_bot: bool = False,
        addressed: bool = False,
    ) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO messages
                (chat_id, message_id, user_id, author, text, reply_to, ts, is_bot, addressed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, user_id, author, text, reply_to, ts, int(is_bot), int(addressed)),
        )
        self.db.commit()

    def recent_messages(self, chat_id: int, limit: int) -> list[StoredMessage]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY message_id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [_message(row) for row in reversed(rows)]

    def messages_after(self, chat_id: int, message_id: int) -> list[StoredMessage]:
        rows = self.db.execute(
            """
            SELECT * FROM messages
            WHERE chat_id = ? AND message_id > ? AND is_bot = 0
            ORDER BY message_id
            """,
            (chat_id, message_id),
        ).fetchall()
        return [_message(row) for row in rows]

    def prune_messages(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - KEEP_MESSAGES_SECONDS
        self.db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.commit()

    # --- сусіди -----------------------------------------------------------

    def set_resident(self, chat_id: int, user_id: int, name: str, apartment: str) -> None:
        self.db.execute(
            """
            INSERT INTO residents (chat_id, user_id, name, apartment, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                name = excluded.name, apartment = excluded.apartment, updated_at = excluded.updated_at
            """,
            (chat_id, user_id, name, apartment, time.time()),
        )
        self.db.commit()

    def forget_resident(self, chat_id: int, user_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM residents WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        self.db.commit()
        return cur.rowcount > 0

    def residents(self, chat_id: int) -> dict[int, Resident]:
        rows = self.db.execute(
            "SELECT user_id, name, apartment FROM residents WHERE chat_id = ?", (chat_id,)
        ).fetchall()
        return {r["user_id"]: Resident(r["user_id"], r["name"], r["apartment"]) for r in rows}

    def needs_reminder(
        self, chat_id: int, user_id: int, *, now: float, repeat_seconds: float, max_count: int
    ) -> bool:
        """Нагадуємо, поки людина не представиться, але не частіше repeat_seconds і не більше max_count разів."""
        row = self.db.execute(
            "SELECT intro_reminded_at, intro_reminder_count FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        if row is None or row["intro_reminded_at"] is None:
            return max_count > 0
        return row["intro_reminder_count"] < max_count and now - row["intro_reminded_at"] >= repeat_seconds

    def mark_reminded(self, chat_id: int, user_ids: list[int], now: float | None = None) -> None:
        now = now or time.time()
        self.db.executemany(
            """
            INSERT INTO members (chat_id, user_id, intro_reminded_at, intro_reminder_count) VALUES (?, ?, ?, 1)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                intro_reminded_at = excluded.intro_reminded_at,
                intro_reminder_count = members.intro_reminder_count + 1
            """,
            [(chat_id, user_id, now) for user_id in user_ids],
        )
        self.db.commit()

    # --- знайомство через особисті ----------------------------------------

    def add_pending(
        self, chat_id: int, user_id: int, greeting_message_id: int | None, *, muted: bool
    ) -> None:
        self.db.execute(
            """
            INSERT INTO pending_intros (chat_id, user_id, greeting_message_id, muted, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                greeting_message_id = COALESCE(excluded.greeting_message_id, pending_intros.greeting_message_id),
                muted = MAX(excluded.muted, pending_intros.muted),
                created_at = excluded.created_at
            """,
            (chat_id, user_id, greeting_message_id, int(muted), time.time()),
        )
        self.db.commit()

    def get_pending(self, chat_id: int, user_id: int) -> PendingIntro | None:
        row = self.db.execute(
            "SELECT * FROM pending_intros WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
        return _pending(row) if row else None

    def pending_for_user(self, user_id: int) -> PendingIntro | None:
        """Останній чат, для якого людина почала знайомство."""
        row = self.db.execute(
            "SELECT * FROM pending_intros WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,)
        ).fetchone()
        return _pending(row) if row else None

    def drop_pending(self, chat_id: int, user_id: int) -> None:
        self.db.execute("DELETE FROM pending_intros WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        self.db.commit()

    def count_muted(self, chat_id: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM pending_intros WHERE chat_id = ? AND muted = 1", (chat_id,)
        ).fetchone()[0]

    def pending_by_greeting(self, chat_id: int, greeting_message_id: int) -> PendingIntro | None:
        row = self.db.execute(
            "SELECT * FROM pending_intros WHERE chat_id = ? AND greeting_message_id = ?",
            (chat_id, greeting_message_id),
        ).fetchone()
        return _pending(row) if row else None

    def muted_before(self, ts: float) -> list[PendingIntro]:
        """Новачки, які чекають знайомства довше, ніж до ts."""
        rows = self.db.execute(
            "SELECT * FROM pending_intros WHERE muted = 1 AND created_at < ?", (ts,)
        ).fetchall()
        return [_pending(row) for row in rows]

    def unmark_muted(self, chat_id: int, user_id: int) -> None:
        self.db.execute(
            "UPDATE pending_intros SET muted = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        self.db.commit()

    # --- події (для лімітів) ----------------------------------------------

    def add_event(self, chat_id: int, kind: str, ts: float | None = None, user_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (chat_id, kind, ts, user_id) VALUES (?, ?, ?, ?)",
            (chat_id, kind, ts or time.time(), user_id),
        )
        self.db.commit()

    def count_user_events(self, user_id: int, kind: str, since: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE user_id = ? AND kind = ? AND ts >= ?", (user_id, kind, since)
        ).fetchone()[0]

    def count_events(self, kind: str, since: float, chat_id: int | None = None) -> int:
        if chat_id is None:
            sql, args = "SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?", (kind, since)
        else:
            sql = "SELECT COUNT(*) FROM events WHERE chat_id = ? AND kind = ? AND ts >= ?"
            args = (chat_id, kind, since)
        return self.db.execute(sql, args).fetchone()[0]

    def last_event(self, chat_id: int, kind: str) -> float | None:
        row = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE chat_id = ? AND kind = ?", (chat_id, kind)
        ).fetchone()
        return row[0]


def _pending(row: sqlite3.Row) -> PendingIntro:
    return PendingIntro(row["chat_id"], row["user_id"], row["greeting_message_id"], bool(row["muted"]))


def _message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        message_id=row["message_id"],
        user_id=row["user_id"],
        author=row["author"],
        text=row["text"],
        reply_to=row["reply_to"],
        ts=row["ts"],
        is_bot=bool(row["is_bot"]),
        addressed=bool(row["addressed"]),
    )
