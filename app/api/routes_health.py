"""Health check routes.

Two probes follow the Kubernetes liveness/readiness convention so Docker and
orchestrators can distinguish "the process is up" from "the process can serve
traffic":

- ``GET /health`` — liveness. Cheap process liveness plus a ``SELECT 1``
  against the configured database. Returns 200 only when the database accepts
  a connection and a query; returns 503 otherwise. The Dockerfile
  ``HEALTHCHECK`` polls this endpoint, so a stale 200 (the previous static
  response) left Docker unaware of a dead database and the container was never
  restarted.

- ``GET /health/ready`` — readiness. Same database probe plus a check that the
  critical runtime tables exist, so a half-bootstrapped instance reports not
  ready instead of accepting work it cannot persist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.core.config import settings
from app.core.logging import logger

router = APIRouter()


def _probe_database() -> tuple[bool, str]:
    """Open a fresh connection and confirm the database answers ``SELECT 1``.

    Uses ``open_connection`` (the canonical factory) rather than the
    thread-local connection so the probe also catches "new connections fail"
    failures — e.g. the WAL file is locked or the database file vanished.
    Returns ``(ok, detail)``.
    """
    try:
        from app.infrastructure.database.connection import open_connection
        conn = open_connection()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - exercised via integration
        logger.warning("[health] database probe failed: %s", exc)
        return False, f"database_unreachable: {exc}"
    return True, "database_ok"


def _critical_tables_present() -> tuple[bool, str]:
    """Confirm the runtime schema is bootstrapped, not just the file present."""
    try:
        from app.infrastructure.database.connection import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name IN ('team_runs', 'task_board_tasks')"
        ).fetchone()
        count = int(row[0]) if row is not None else 0
    except Exception as exc:  # pragma: no cover - exercised via integration
        return False, f"schema_probe_failed: {exc}"
    if count < 2:
        return False, f"schema_incomplete: found {count}/2 critical tables"
    return True, "schema_ok"


@router.get("/health")
def health_check(response: Response) -> dict[str, Any]:
    db_ok, db_detail = _probe_database()
    status = "ok" if db_ok else "degraded"
    response.status_code = 200 if db_ok else 503
    return {
        "status": status,
        "app": settings.app_name,
        "database": db_detail,
    }


@router.get("/health/ready")
def readiness_check(response: Response) -> dict[str, Any]:
    db_ok, db_detail = _probe_database()
    schema_ok, schema_detail = _critical_tables_present()
    ready = db_ok and schema_ok
    response.status_code = 200 if ready else 503
    return {
        "ready": ready,
        "app": settings.app_name,
        "database": db_detail,
        "schema": schema_detail,
    }
