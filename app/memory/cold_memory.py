"""冷记忆 SQLite 存储：sessions、messages、tool_calls 表。"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings


_local = threading.local()


def get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = Path(settings.sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _init_db(_local.conn)
    return _local.conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_calls TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            tool_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            message_id INTEGER,
            tool_name TEXT NOT NULL,
            arguments TEXT DEFAULT '{}',
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_session_id ON tool_calls(session_id);
    """)
    conn.commit()


class ColdMemory:
    def __init__(self):
        self.conn = get_connection()

    def create_session(self, session_id: str, metadata: dict | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at, metadata) VALUES (?, ?, ?)",
            (session_id, datetime.utcnow().isoformat(), json.dumps(metadata or {})),
        )
        self.conn.commit()

    def add_message(self, session_id: str, role: str, content: str, tool_calls: list | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                json.dumps(tool_calls or [], ensure_ascii=False),
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_tool_call(self, session_id: str, tool_name: str, arguments: dict, result: str | None = None, error: str | None = None, message_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO tool_calls (session_id, message_id, tool_name, arguments, result, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                message_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False),
                result,
                error,
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """在 messages 和 tool_calls 中搜索。"""
        rows = []
        cur = self.conn.execute(
            """SELECT m.session_id, m.role, m.content, m.created_at
               FROM messages m
               WHERE m.content LIKE ?
               ORDER BY m.created_at DESC
               LIMIT ?""",
            (f"%{query}%", limit),
        )
        for row in cur.fetchall():
            rows.append({
                "type": "message",
                "session_id": row["session_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            })

        cur = self.conn.execute(
            """SELECT tc.session_id, tc.tool_name, tc.arguments, tc.result, tc.created_at
               FROM tool_calls tc
               WHERE tc.tool_name LIKE ? OR tc.result LIKE ?
               ORDER BY tc.created_at DESC
               LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit),
        )
        for row in cur.fetchall():
            rows.append({
                "type": "tool_call",
                "session_id": row["session_id"],
                "tool_name": row["tool_name"],
                "arguments": row["arguments"],
                "result": row["result"],
                "created_at": row["created_at"],
            })

        return rows


_cold_memory: ColdMemory | None = None


def get_cold_memory() -> ColdMemory:
    global _cold_memory
    if _cold_memory is None:
        _cold_memory = ColdMemory()
    return _cold_memory
