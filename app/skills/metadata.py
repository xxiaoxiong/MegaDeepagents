"""Skill 元数据与使用统计。"""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger


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
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            description TEXT,
            created_by TEXT NOT NULL DEFAULT 'user',
            source TEXT NOT NULL DEFAULT 'local',
            state TEXT NOT NULL DEFAULT 'active',
            pinned INTEGER NOT NULL DEFAULT 0,
            bundled INTEGER NOT NULL DEFAULT 0,
            hub_installed INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            content_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT,
            archived_at TEXT
        );

        CREATE TABLE IF NOT EXISTS skill_usage_events (
            id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL,
            task_id TEXT,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
        CREATE INDEX IF NOT EXISTS idx_skill_usage_skill_id ON skill_usage_events(skill_id);
        CREATE INDEX IF NOT EXISTS idx_skill_usage_task_id ON skill_usage_events(task_id);
    """)
    conn.commit()


def compute_content_hash(path: Path) -> str:
    h = hashlib.sha256()
    skill_md = path / "SKILL.md"
    if skill_md.exists():
        h.update(skill_md.read_bytes())
    return h.hexdigest()[:16]


def register_skill(name: str, path: Path, description: str, created_by: str = "user",
                   source: str = "local", state: str = "active", pinned: bool = False,
                   bundled: bool = False, hub_installed: bool = False) -> dict[str, Any]:
    conn = get_connection()
    now = datetime.utcnow().isoformat()
    skill_id = f"skill-{name}"
    content_hash = compute_content_hash(path)
    conn.execute(
        """INSERT OR REPLACE INTO skills
           (id, name, path, description, created_by, source, state, pinned, bundled,
            hub_installed, version, content_hash, created_at, updated_at, last_used_at, archived_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            skill_id,
            name,
            str(path),
            description,
            created_by,
            source,
            state,
            1 if pinned else 0,
            1 if bundled else 0,
            1 if hub_installed else 0,
            1,
            content_hash,
            now,
            now,
            None,
            None,
        ),
    )
    conn.commit()
    return {
        "id": skill_id,
        "name": name,
        "path": str(path),
        "description": description,
        "created_by": created_by,
        "source": source,
        "state": state,
        "pinned": pinned,
        "bundled": bundled,
        "hub_installed": hub_installed,
        "version": 1,
        "content_hash": content_hash,
        "created_at": now,
        "updated_at": now,
    }


def update_skill_state(name: str, **updates) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    set_clauses = []
    values = []
    for key, val in updates.items():
        if key == "pinned":
            val = 1 if val else 0
        if key == "bundled":
            val = 1 if val else 0
        if key == "hub_installed":
            val = 1 if val else 0
        if key == "archived_at" and val is not None:
            val = val if isinstance(val, str) else val.isoformat()
        set_clauses.append(f"{key} = ?")
        values.append(val)
    values.append(name)
    conn.execute(f"UPDATE skills SET {', '.join(set_clauses)} WHERE name = ?", values)
    conn.commit()
    updated = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
    return dict(updated)


def get_skill(name: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["pinned"] = bool(d["pinned"])
    d["bundled"] = bool(d["bundled"])
    d["hub_installed"] = bool(d["hub_installed"])
    return d


def list_skills(state: str | None = None, created_by: str | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    query = "SELECT * FROM skills WHERE 1=1"
    params: list = []
    if state:
        query += " AND state = ?"
        params.append(state)
    if created_by:
        query += " AND created_by = ?"
        params.append(created_by)
    rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["pinned"] = bool(d["pinned"])
        d["bundled"] = bool(d["bundled"])
        d["hub_installed"] = bool(d["hub_installed"])
        result.append(d)
    return result


def record_usage(name: str, event_type: str, task_id: str | None = None, metadata: dict | None = None):
    conn = get_connection()
    now = datetime.utcnow().isoformat()
    event_id = f"evt-{datetime.utcnow().timestamp():.0f}-{name}"
    conn.execute(
        """INSERT INTO skill_usage_events (id, skill_id, task_id, event_type, created_at, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, f"skill-{name}", task_id, event_type, now, json.dumps(metadata or {})),
    )
    # 更新 last_used_at
    conn.execute("UPDATE skills SET last_used_at = ? WHERE name = ?", (now, name))
    conn.commit()


def get_usage_stats(name: str) -> dict[str, Any]:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) FROM skill_usage_events WHERE skill_id = ?", (f"skill-{name}",)
    ).fetchone()[0]
    by_type: dict[str, int] = {}
    rows = conn.execute(
        """SELECT event_type, COUNT(*) as cnt FROM skill_usage_events
           WHERE skill_id = ? GROUP BY event_type""",
        (f"skill-{name}",),
    ).fetchall()
    for r in rows:
        by_type[r[0]] = r[1]
    return {"skill_name": name, "total_events": total, "by_type": by_type}
