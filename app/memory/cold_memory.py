"""冷记忆 SQLite 存储：sessions、messages、tool_calls 表。"""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.infrastructure.database.connection import get_connection as get_database_connection
from app.memory.pii_filter import redact


# Track which connection objects have already been bootstrapped with the
# cold-memory schema.  The canonical connection is thread-local and shared
# with every other subsystem, so the DDL only needs to run once per connection
# object (idempotent ``CREATE TABLE IF NOT EXISTS`` either way).
_initialized_connection_ids: set[int] = set()


def get_connection() -> sqlite3.Connection:
    """Return the canonical application connection, bootstrapping cold-memory schema.

    Previously this opened an independent ``sqlite3.connect`` to
    ``settings.sqlite_path`` without the WAL/busy_timeout/foreign_keys pragmas,
    violating AGENTS.md ("Do not open an independent application database").
    The independent connection also competed with the application connection
    for the WAL writer lock and its transactions escaped the canonical
    ``transaction()`` boundary.  Reusing the canonical connection keeps one
    connection per thread, with consistent pragmas and a single transaction
    domain.
    """
    connection = get_database_connection()
    connection_id = id(connection)
    if connection_id not in _initialized_connection_ids:
        _init_db(connection)
        _initialized_connection_ids.add(connection_id)
    return connection


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
            (session_id, datetime.now(UTC).isoformat(), json.dumps(metadata or {})),
        )
        self.conn.commit()

    def add_message(self, session_id: str, role: str, content: str, tool_calls: list | None = None) -> int:
        # Redact secrets before they reach durable storage.  Agent messages
        # can contain API keys (e.g. pasted during debugging), and unlike the
        # event envelope path (``_redact_event_payload``) cold_memory
        # previously stored content verbatim — then ``session_search`` would
        # return the raw key to the LLM.  ``redact`` only substitutes known
        # secret patterns with ``[REDACTED]``, leaving normal prose intact.
        safe_content = redact(content) if isinstance(content, str) else content
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                safe_content,
                json.dumps(tool_calls or [], ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_tool_call(self, session_id: str, tool_name: str, arguments: dict, result: str | None = None, error: str | None = None, message_id: int | None = None) -> int:
        # ``result`` is free-form tool output (stdout, file contents) and is
        # the most likely vector for a leaked secret to land in cold_memory;
        # redact it on write.  ``arguments`` is structured JSON and is left
        # intact so downstream replay parsing stays valid.
        safe_result = redact(result) if isinstance(result, str) else result
        cur = self.conn.execute(
            "INSERT INTO tool_calls (session_id, message_id, tool_name, arguments, result, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                message_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False),
                safe_result,
                error,
                datetime.now(UTC).isoformat(),
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


def reset_cold_memory() -> None:
    """Drop the cached singleton and per-connection bootstrap state.

    Tests close and reopen the canonical SQLite connection every case
    (``tests/conftest.py::_isolate_multiagent_store``).  Without this reset
    the ``_cold_memory`` singleton would keep holding a closed connection and
    the ``_initialized_connection_ids`` cache would falsely skip schema
    bootstrap when a new connection reuses a freed id() — the same
    stale-cache failure mode that affected ``event_envelopes``.  Production
    never calls this, so caching still wins there.
    """
    global _cold_memory
    _cold_memory = None
    _initialized_connection_ids.clear()
