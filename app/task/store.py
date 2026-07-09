"""SQLite 任务存储：tasks、task_events、task_messages、artifacts 表，以及 skills 基础表。"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.task.models import ArtifactInfo, Task, TaskEvent, TaskMessage, TaskStatus


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
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            user_input TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            thread_id TEXT NOT NULL DEFAULT 'default',
            final_answer TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'assistant',
            content TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

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

        CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_messages_task_id ON task_messages(task_id);
        CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id);
        CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
        CREATE INDEX IF NOT EXISTS idx_skill_usage_skill_id ON skill_usage_events(skill_id);
        CREATE INDEX IF NOT EXISTS idx_skill_usage_task_id ON skill_usage_events(task_id);
    """)
    conn.commit()


def close_connection() -> None:
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


class TaskStore:
    def __init__(self):
        self.conn = get_connection()

    def create_task(self, task: Task) -> Task:
        self.conn.execute(
            """INSERT INTO tasks (task_id, user_input, status, thread_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                task.task_id,
                task.user_input,
                task.status.value,
                task.thread_id,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )
        self.conn.commit()
        return task

    def get_task(self, task_id: str) -> Task | None:
        cur = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def update_task(self, task_id: str, **updates) -> Task | None:
        task = self.get_task(task_id)
        if not task:
            return None

        set_clauses = []
        values = []
        for key, val in updates.items():
            if key == "status" and isinstance(val, TaskStatus):
                val = val.value
            set_clauses.append(f"{key} = ?")
            values.append(val)

        values.append(datetime.utcnow().isoformat())
        values.append(task_id)

        sql = f"UPDATE tasks SET {', '.join(set_clauses)}, updated_at = ? WHERE task_id = ?"
        self.conn.execute(sql, values)
        self.conn.commit()
        return self.get_task(task_id)

    def add_event(self, task_id: str, event: TaskEvent) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, event_type, data, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                event.event_type,
                json.dumps(event.data, ensure_ascii=False),
                event.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def add_message(self, task_id: str, message: TaskMessage) -> None:
        self.conn.execute(
            "INSERT INTO task_messages (task_id, role, content, extra, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                message.role,
                message.content,
                json.dumps(message.extra, ensure_ascii=False),
                message.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_messages(self, task_id: str) -> list[TaskMessage]:
        cur = self.conn.execute(
            "SELECT role, content, extra, created_at FROM task_messages WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        messages = []
        for row in cur.fetchall():
            extra = {}
            try:
                raw_extra = row["extra"]
                if raw_extra:
                    extra = json.loads(raw_extra)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Invalid JSON in message extra for task {task_id}: {row['extra']!r}")
            messages.append(TaskMessage(
                role=row["role"],
                content=row["content"],
                extra=extra,
                created_at=datetime.fromisoformat(row["created_at"]),
            ))
        return messages

    def add_artifact(self, task_id: str, artifact: ArtifactInfo) -> None:
        self.conn.execute(
            "INSERT INTO artifacts (task_id, path, name, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                artifact.path,
                artifact.name,
                artifact.size_bytes,
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()

    def get_events(self, task_id: str) -> list[TaskEvent]:
        cur = self.conn.execute(
            "SELECT event_type, data, created_at FROM task_events WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        events = []
        for row in cur.fetchall():
            data = {}
            try:
                raw_data = row["data"]
                if raw_data:
                    data = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Invalid JSON in event data for task {task_id}: {row['data']!r}")
            events.append(TaskEvent(
                event_type=row["event_type"],
                data=data,
                created_at=datetime.fromisoformat(row["created_at"]),
            ))
        return events

    def get_artifacts(self, task_id: str) -> list[ArtifactInfo]:
        cur = self.conn.execute(
            "SELECT path, name, size_bytes FROM artifacts WHERE task_id = ?",
            (task_id,),
        )
        return [
            ArtifactInfo(
                path=row["path"],
                name=row["name"],
                size_bytes=row["size_bytes"],
            )
            for row in cur.fetchall()
        ]

    def delete_task(self, task_id: str) -> bool:
        cur = self.conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,))
        if not cur.fetchone():
            return False
        self.conn.execute("DELETE FROM task_messages WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        self.conn.commit()
        return True

    def list_tasks(self, limit: int = 20) -> list[Task]:
        cur = self.conn.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_task(row) for row in cur.fetchall()]

    def _row_to_task(self, row: sqlite3.Row, include_relations: bool = False) -> Task:
        task = Task(
            task_id=row["task_id"],
            user_input=row["user_input"],
            status=TaskStatus(row["status"]),
            thread_id=row["thread_id"],
            final_answer=row["final_answer"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        if include_relations:
            task.artifacts = self.get_artifacts(task.task_id)
            task.events = self.get_events(task.task_id)
            task.messages = self.get_messages(task.task_id)
        return task

    def get_task_full(self, task_id: str) -> Task | None:
        """获取任务并加载关联数据（events/messages/artifacts）。"""
        cur = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_task(row, include_relations=True)


_thread_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _thread_store
    if _thread_store is None:
        _thread_store = TaskStore()
    return _thread_store
