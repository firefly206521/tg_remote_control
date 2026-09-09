"""Persistent conversation history shared by Telegram and the local web UI."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'message',
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session "
            "ON messages(chat_id, session_key, id)"
        )
        self._db.commit()

    def append(self, chat_id: int, session_key: str, role: str, text: str, kind: str = "message") -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO messages(chat_id, session_key, role, kind, text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, session_key, role, kind, text, time.time()),
            )
            self._db.commit()
            return int(cursor.lastrowid)

    def list_messages(self, chat_id: int, session_key: str, after: int = 0, limit: int = 200) -> list[dict]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT id, role, kind, text, created_at FROM messages "
                "WHERE chat_id = ? AND session_key = ? AND id > ? ORDER BY id LIMIT ?",
                (chat_id, session_key, max(0, int(after)), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
