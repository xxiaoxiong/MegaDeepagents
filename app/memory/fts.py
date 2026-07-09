"""FTS5 搜索：中文 trigram + 英文 unicode61 混合检索。"""

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.core.config import settings


_local = threading.local()


def get_cold_memory_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = Path(settings.sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _init_fts(_local.conn)
    return _local.conn


def _init_fts(conn: sqlite3.Connection) -> None:
    # 检查是否已有 FTS 表
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'")
    if cur.fetchone():
        return

    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            tokenize='unicode61',
            content='messages'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
            content,
            tokenize='trigram',
            content='messages'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
            INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
            INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content) VALUES ('delete', old.rowid, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
            INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content) VALUES ('delete', old.rowid, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
            INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
        END;
    """)
    conn.commit()


def query_sanitize(query: str) -> str:
    """移除 FTS5 特殊字符，防止语法错误。"""
    query = re.sub(r'["*\-+~^():]', ' ', query)
    query = re.sub(r'\s+', ' ', query).strip()
    return query


def is_cjk(text: str) -> bool:
    """判断是否包含 CJK 字符。"""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            return True
    return False


def search_fts(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """混合搜索：CJK 三字以上走 trigram，英文走 unicode61，1-2 字 CJK 走 LIKE 兜底。"""
    conn = get_cold_memory_conn()
    sanitized = query_sanitize(query)
    if not sanitized:
        return []

    results: list[dict[str, Any]] = []

    if is_cjk(query):
        cjk_chars = [c for c in query if is_cjk(c)]
        if len(cjk_chars) <= 2:
            # 1-2 字 CJK 走 LIKE 兜底
            cur = conn.execute(
                """SELECT m.session_id, m.role, m.content, m.created_at
                   FROM messages m
                   WHERE m.content LIKE ?
                   ORDER BY m.created_at DESC
                   LIMIT ?""",
                (f"%{query}%", limit),
            )
            for row in cur.fetchall():
                results.append({
                    "type": "message",
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "rank": "like_fallback",
                })
        else:
            # 三字以上走 trigram
            try:
                cur = conn.execute(
                    """SELECT m.rowid, m.session_id, m.role, m.content, m.created_at
                       FROM messages_fts_trigram fts
                       JOIN messages m ON m.rowid = fts.rowid
                       WHERE messages_fts_trigram MATCH ?
                       ORDER BY m.created_at DESC
                       LIMIT ?""",
                    (sanitized, limit),
                )
                for row in cur.fetchall():
                    results.append({
                        "type": "message",
                        "session_id": row["session_id"],
                        "role": row["role"],
                        "content": row["content"],
                        "created_at": row["created_at"],
                        "rank": "trigram",
                    })
            except sqlite3.OperationalError:
                pass
    else:
        # 英文走 unicode61
        try:
            cur = conn.execute(
                """SELECT m.rowid, m.session_id, m.role, m.content, m.created_at
                   FROM messages_fts fts
                   JOIN messages m ON m.rowid = fts.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY m.created_at DESC
                   LIMIT ?""",
                (sanitized, limit),
            )
            for row in cur.fetchall():
                results.append({
                    "type": "message",
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "rank": "unicode61",
                })
        except sqlite3.OperationalError:
            pass

    return results
