"""One SQLite connection policy for every application subsystem.

The connection remains thread-local because workers execute concurrently, while
all connections point at the same configured database and use the same safety
pragmas.  Domain and API modules never open SQLite directly.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.core.config import settings


_local = threading.local()


def utc_now_iso() -> str:
    """Return the canonical timestamp representation used in persisted rows."""
    return datetime.now(UTC).isoformat()


def get_connection() -> sqlite3.Connection:
    """Return the current thread's configured connection."""
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = open_connection()
        _local.connection = connection
    return connection


def open_connection() -> sqlite3.Connection:
    """Open a policy-compliant dedicated connection.

    Components such as LangGraph's checkpointer manage their own transaction
    boundary and therefore must not share the application thread's connection.
    They still use this one factory, database path, and pragma policy.
    """
    database_path = Path(settings.sqlite_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(database_path),
        timeout=settings.sqlite_busy_timeout_ms / 1000,
        check_same_thread=False,
        isolation_level="DEFERRED",
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=NORMAL")
    _migrate_legacy_artifact_table(connection)
    return connection


def _migrate_legacy_artifact_table(connection: sqlite3.Connection) -> None:
    """Move V1 task attachments away from the V3 artifact registry name."""
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='artifacts'"
    ).fetchone()
    if table is None:
        return
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
    }
    if "artifact_id" in columns or not {"task_id", "path", "name"}.issubset(columns):
        return
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_artifacts'"
    ).fetchone()
    if existing is None:
        connection.execute("ALTER TABLE artifacts RENAME TO task_artifacts")
    else:
        connection.execute(
            """INSERT INTO task_artifacts (task_id, path, name, size_bytes, created_at)
               SELECT task_id, path, name, size_bytes, created_at FROM artifacts"""
        )
        connection.execute("DROP TABLE artifacts")
    connection.commit()


@contextmanager
def transaction(*, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Run a transaction with explicit commit/rollback semantics."""
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None
