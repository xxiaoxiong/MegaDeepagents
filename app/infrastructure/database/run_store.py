"""V3 runtime persistence for runs, tasks, agents, events, and approvals.

设计：
- 复用 store.py 的 SQLite 连接（线程本地），不引入新库
- 复用旧表并提供显式迁移，避免破坏已有单机数据
- 所有写入均带 created_at/updated_at；
- 提供 resume_run / load_completed_tasks 等恢复能力
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import logger
from app.memory.pii_filter import redact
from app.multiagent.store import _get_conn


_SENSITIVE_EVENT_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
})


def _redact_event_payload(value: Any, key: str | None = None) -> Any:
    """Remove credentials before an audit event reaches durable storage."""
    normalized_key = (key or "").strip().lower().replace("-", "_")
    if normalized_key in _SENSITIVE_EVENT_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_event_payload(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_event_payload(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def make_run_event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:12]


def make_permission_request_id() -> str:
    return "preq_" + uuid.uuid4().hex[:12]


def make_task_run_id() -> str:
    return "trun_" + uuid.uuid4().hex[:12]


_EVENT_ENVELOPES_DDL = """CREATE TABLE IF NOT EXISTS event_envelopes (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent_id TEXT,
    task_id TEXT, event_type TEXT NOT NULL, sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL, payload TEXT NOT NULL, trace_id TEXT,
    idempotency_key TEXT,
    UNIQUE(run_id, sequence)
)"""

# Partial unique index: only rows that actually carry an idempotency_key are
# constrained.  Existing rows (and rows emitted without a key) keep NULL and
# are unaffected, so this is safe to add to a live database.
_EVENT_ENVELOPES_IDEM_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_envelopes_idem "
    "ON event_envelopes(idempotency_key) WHERE idempotency_key IS NOT NULL"
)

# ``ALTER TABLE ADD COLUMN`` is needed for databases that already have the
# table from an older build (CREATE TABLE IF NOT EXISTS will not add the
# column).  Detect via PRAGMA table_info so the ALTER only runs once.
_EVENT_ENVELOPES_ADD_IDEM_COLUMN = (
    "ALTER TABLE event_envelopes ADD COLUMN idempotency_key TEXT"
)


# Track which connection objects have already been bootstrapped with the
# event_envelopes schema.  ``CREATE TABLE IF NOT EXISTS`` is idempotent but
# SQLite still parses and plans it on every call; on the SSE hot path
# (``list_event_envelopes`` polled at ~5 Hz) and on every ``record_event``
# that parsing showed up as measurable write amplification.  The canonical
# connection is thread-local so this set is only consulted by one thread.
_event_envelopes_initialized_conns: set[int] = set()


def _ensure_event_envelopes(conn) -> None:
    """Create the ``event_envelopes`` table if it does not yet exist.

    Centralised so the canonical SSE-replay table is defined in one place
    instead of being re-declared on every hot-path read/write.  The DDL only
    runs once per connection object; subsequent calls are a set lookup.
    """
    conn_id = id(conn)
    if conn_id in _event_envelopes_initialized_conns:
        return
    conn.execute(_EVENT_ENVELOPES_DDL)
    # Add the idempotency_key column to pre-existing tables (older builds
    # created the table without it).  PRAGMA table_info lets us detect the
    # missing column cheaply; ALTER TABLE ADD COLUMN is idempotent-safe
    # because we only run it when the column is absent.
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(event_envelopes)").fetchall()
        }
        if "idempotency_key" not in columns:
            conn.execute(_EVENT_ENVELOPES_ADD_IDEM_COLUMN)
    except Exception:
        # If the ALTER fails (e.g. column somehow already exists under a
        # concurrent migration), the partial index below still works on
        # fresh tables and on tables that already have the column.
        pass
    conn.execute(_EVENT_ENVELOPES_IDEM_INDEX_DDL)
    _event_envelopes_initialized_conns.add(conn_id)


def _append_event_envelope(
    conn,
    *,
    event_id: str,
    run_id: str,
    event_type: str,
    agent_id: str | None = None,
    task_id: str | None = None,
    task_run_id: str | None = None,
    trace_id: str | None = None,
    occurred_at: str | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> int:
    """Append one event to ``team_events`` + ``event_envelopes`` atomically.

    This is the transactional outbox writer.  The caller MUST already hold a
    ``BEGIN IMMEDIATE`` transaction on ``conn`` so the event is committed in
    the same statement as the state change it describes — that is what makes
    the event visible to the SSE stream (which reads ``event_envelopes``)
    without a separate consumer/ACK loop.  The previous ``control_plane_outbox``
    table wrote rows here but never drained them, so control-plane events
    (TaskCreated / TaskGraphReplanned / TaskGraphMutation) were invisible to
    SSE; this helper replaces that dead infrastructure.

    When ``idempotency_key`` is supplied, a prior event with the same key
    short-circuits: the existing sequence is returned and no duplicate row is
    inserted.  This protects budget restoration (``TaskToolBudgetConsumed``)
    and other replay-sensitive callers from double-counting when the same
    logical event is emitted more than once.

    Returns the assigned per-run sequence number.
    """
    timestamp = occurred_at or datetime.now(UTC).isoformat()
    safe_payload = _redact_event_payload(payload or {})
    payload_json = json.dumps(safe_payload)
    # Idempotency: if a prior event with the same key already exists, return
    # its sequence and skip the insert.  The partial unique index
    # ``idx_event_envelopes_idem`` is the backstop in case two callers race
    # inside separate transactions (the second INSERT would raise
    # IntegrityError, which the caller's rollback handles).
    if idempotency_key is not None:
        existing = conn.execute(
            "SELECT sequence FROM event_envelopes WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return int(existing["sequence"])
    conn.execute(
        """INSERT INTO team_events (
            event_id, run_id, event_type, agent_id, task_id, task_run_id,
            timestamp, trace_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, run_id, event_type, agent_id, task_id, task_run_id,
         timestamp, trace_id, payload_json),
    )
    sequence = int(conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM event_envelopes WHERE run_id=?",
        (run_id,),
    ).fetchone()["seq"])
    conn.execute(
        """INSERT INTO event_envelopes (
            event_id, run_id, agent_id, task_id, event_type, sequence,
            timestamp, payload, trace_id, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, run_id, agent_id, task_id, event_type, sequence,
         timestamp, payload_json, trace_id, idempotency_key),
    )
    return sequence


class AgentRunHistory:
    """AgentInstance、TaskRun、TeamEvent 持久化接口。

    所有方法假定调用方已保证 conn 准备好（即 _get_conn() 可用）。
    """

    @property
    def conn(self):
        return _get_conn()

    # ===== TeamRun control plane =====

    def save_team_run(self, *, run_id: str, goal: str, team_id: str, mode: str,
                      workspace_root: str, status: str, max_rounds: int,
                      review_required: bool, metadata: dict[str, Any] | None = None) -> None:
        _ensure_team_runs(self.conn)
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO team_runs (run_id, goal, team_id, mode, workspace_root, status,
               max_rounds, review_required, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
               metadata=excluded.metadata, updated_at=excluded.updated_at""",
            (run_id, goal, team_id, mode, workspace_root, status, max_rounds,
             int(review_required), json.dumps(metadata or {}), now, now),
        )
        self.conn.commit()

    def get_team_run(self, run_id: str) -> dict[str, Any] | None:
        _ensure_team_runs(self.conn)
        row = self.conn.execute("SELECT * FROM team_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def update_team_run_status(self, run_id: str, status: str) -> bool:
        _ensure_team_runs(self.conn)
        cur = self.conn.execute("UPDATE team_runs SET status=?, updated_at=? WHERE run_id=?",
                                (status, datetime.now(UTC).isoformat(), run_id))
        self.conn.commit()
        return cur.rowcount > 0

    def acquire_team_run_execution_lease(
        self,
        run_id: str,
        lease_id: str,
        *,
        ttl_seconds: float = 60.0,
        allowed_statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically fence checkpoint execution across API workers.

        The lease lives in existing Run metadata to avoid a second control
        plane.  ``BEGIN IMMEDIATE`` serializes competing claimers; expiration
        plus heartbeats allows recovery after a process crash.
        """
        _ensure_team_runs(self.conn)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=max(1.0, ttl_seconds))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM team_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return None
            record = _row_to_dict(row)
            if (
                allowed_statuses is not None
                and str(record.get("status")) not in allowed_statuses
            ):
                self.conn.rollback()
                return None
            metadata = dict(record.get("metadata") or {})
            existing = metadata.get("execution_lease") or {}
            existing_id = str(existing.get("lease_id") or "")
            existing_expiry = self._parse_lease_timestamp(
                existing.get("expires_at")
            )
            if (
                existing_id
                and existing_id != lease_id
                and existing_expiry is not None
                and existing_expiry > now
            ):
                self.conn.rollback()
                return None
            metadata["execution_lease"] = {
                "lease_id": lease_id,
                "acquired_at": now.isoformat(),
                "heartbeat_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            self.conn.execute(
                "UPDATE team_runs SET metadata=?, updated_at=? WHERE run_id=?",
                (json.dumps(metadata), now.isoformat(), run_id),
            )
            self.conn.commit()
            record["metadata"] = metadata
            return record
        except Exception:
            self.conn.rollback()
            raise

    def refresh_team_run_execution_lease(
        self,
        run_id: str,
        lease_id: str,
        *,
        ttl_seconds: float = 60.0,
    ) -> bool:
        _ensure_team_runs(self.conn)
        now = datetime.now(UTC)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT metadata FROM team_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return False
            metadata = json.loads(row["metadata"] or "{}")
            lease = dict(metadata.get("execution_lease") or {})
            if str(lease.get("lease_id") or "") != lease_id:
                self.conn.rollback()
                return False
            lease["heartbeat_at"] = now.isoformat()
            lease["expires_at"] = (
                now + timedelta(seconds=max(1.0, ttl_seconds))
            ).isoformat()
            metadata["execution_lease"] = lease
            self.conn.execute(
                "UPDATE team_runs SET metadata=?, updated_at=? WHERE run_id=?",
                (json.dumps(metadata), now.isoformat(), run_id),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def release_team_run_execution_lease(
        self,
        run_id: str,
        lease_id: str,
    ) -> bool:
        _ensure_team_runs(self.conn)
        now = datetime.now(UTC)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT metadata FROM team_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return False
            metadata = json.loads(row["metadata"] or "{}")
            lease = dict(metadata.get("execution_lease") or {})
            if str(lease.get("lease_id") or "") != lease_id:
                self.conn.rollback()
                return False
            metadata.pop("execution_lease", None)
            self.conn.execute(
                "UPDATE team_runs SET metadata=?, updated_at=? WHERE run_id=?",
                (json.dumps(metadata), now.isoformat(), run_id),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _parse_lease_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def merge_team_run_metadata(
        self, run_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Merge operational metadata without replacing the durable Run.

        Previously this performed a read-modify-write outside any transaction:
        ``get_team_run`` read ``metadata``, then a separate ``UPDATE`` wrote
        it back.  ``acquire_team_run_execution_lease`` / ``refresh_*`` write
        ``metadata.execution_lease`` inside ``BEGIN IMMEDIATE``; if either of
        those ran between the read and the write here, the lease would be
        silently overwritten and the scheduler's lease-lost detector would
        cancel a still-running Run.  Wrap the whole merge in
        ``BEGIN IMMEDIATE`` so the read and the write are one atomic step.
        """
        _ensure_team_runs(self.conn)
        now = datetime.now(UTC)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT metadata FROM team_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return None
            metadata = json.loads(row["metadata"] or "{}")
            metadata.update(updates)
            self.conn.execute(
                "UPDATE team_runs SET metadata=?, updated_at=? WHERE run_id=?",
                (json.dumps(metadata), now.isoformat(), run_id),
            )
            self.conn.commit()
            return metadata
        except Exception:
            self.conn.rollback()
            raise

    def list_team_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List durable runs for one unified API control plane."""
        _ensure_team_runs(self.conn)
        rows = self.conn.execute(
            "SELECT * FROM team_runs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # ===== TaskBoard durable data plane =====

    def upsert_task_board_task(self, payload: dict[str, Any]) -> None:
        """Persist the complete TaskBoard record using its run/task composite key."""
        _ensure_task_board_tasks(self.conn)
        self.conn.execute(
            """INSERT INTO task_board_tasks (run_id, task_id, payload, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id, task_id) DO UPDATE SET
                 payload=excluded.payload, updated_at=excluded.updated_at""",
            (
                payload["run_id"], payload["task_id"], json.dumps(payload),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def list_task_board_tasks(self, run_id: str) -> list[dict[str, Any]]:
        """Load the authoritative board state for one run after a restart."""
        _ensure_task_board_tasks(self.conn)
        rows = self.conn.execute(
            "SELECT payload FROM task_board_tasks WHERE run_id=? ORDER BY rowid ASC",
            (run_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                result.append(json.loads(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                logger.warning("[RunStore] skipped corrupt persisted TaskBoard row run=%s", run_id)
        return result

    def find_task_board_task(
        self, task_id: str, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Find a durable task without guessing when local task ids collide."""
        _ensure_task_board_tasks(self.conn)
        if run_id is None:
            rows = self.conn.execute(
                "SELECT payload FROM task_board_tasks WHERE task_id=? ORDER BY updated_at DESC",
                (task_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload FROM task_board_tasks WHERE task_id=? AND run_id=?",
                (task_id, run_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                result.append(json.loads(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                logger.warning("[RunStore] skipped corrupt persisted task=%s", task_id)
        return result

    # ===== Durable TaskGraph snapshots =====

    def save_task_graph(self, run_id: str, graph: dict[str, Any]) -> None:
        """Store the complete versioned plan, not merely its TaskBoard projection.

        TaskBoard is authoritative for claims and attempts.  The graph retains
        contracts, budgets, artifact lineage and plan revision data required to
        resume verification/replanning without silently changing the plan.
        """
        _ensure_task_graph_snapshots(self.conn)
        version = int(graph.get("version", 1))
        self.conn.execute(
            """INSERT INTO task_graph_snapshots (run_id, version, graph_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET version=excluded.version,
                 graph_json=excluded.graph_json, updated_at=excluded.updated_at""",
            (run_id, version, json.dumps(graph), datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def load_task_graph(self, run_id: str) -> dict[str, Any] | None:
        """Return the last complete TaskGraph snapshot for a run."""
        _ensure_task_graph_snapshots(self.conn)
        row = self.conn.execute(
            "SELECT graph_json FROM task_graph_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["graph_json"])
        except (TypeError, json.JSONDecodeError):
            logger.warning("[RunStore] corrupt TaskGraph snapshot run=%s", run_id)
            return None

    # ===== AgentInstance =====

    def upsert_agent_instance(
        self,
        agent_id: str,
        team_id: str,
        run_id: str,
        profile_id: str,
        name: str,
        role: str,
        session_id: str,
        thread_id: str,
        checkpoint_namespace: str,
        status: str,
        current_task_id: str | None = None,
        workspace_root: str = "",
        last_heartbeat_at: datetime | None = None,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        stopped_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO agent_instances (
                agent_id, team_id, run_id, profile_id, name, role,
                session_id, thread_id, checkpoint_namespace, status, current_task_id,
                workspace_root, last_heartbeat_at, capabilities, metadata,
                created_at, updated_at, stopped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                status=excluded.status,
                current_task_id=excluded.current_task_id,
                last_heartbeat_at=excluded.last_heartbeat_at,
                capabilities=excluded.capabilities,
                metadata=excluded.metadata,
                updated_at=excluded.updated_at,
                stopped_at=excluded.stopped_at
            """,
            (
                agent_id, team_id, run_id, profile_id, name, role,
                session_id, thread_id, checkpoint_namespace, status, current_task_id,
                workspace_root,
                last_heartbeat_at.isoformat() if last_heartbeat_at else None,
                json.dumps(capabilities or []),
                json.dumps(metadata or {}),
                (created_at or datetime.now(UTC)).isoformat(),
                now,
                stopped_at.isoformat() if stopped_at else None,
            ),
        )
        self.conn.commit()

    def get_agent_instance(self, agent_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM agent_instances WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_by_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_instances WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_alive(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """跨重启存活可恢复的 Agent（非 STOPPED/FAILED）。"""
        if run_id:
            rows = self.conn.execute(
                "SELECT * FROM agent_instances WHERE run_id = ? AND status NOT IN ('stopped', 'failed')",
                (run_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM agent_instances WHERE status NOT IN ('stopped', 'failed')"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ===== TaskRun =====

    def insert_task_run(
        self,
        task_run_id: str,
        task_id: str,
        agent_id: str,
        run_id: str,
        attempt: int = 1,
        status: str = "created",
        checkpoint_id: str | None = None,
        artifact_ids: list[str] | None = None,
        tool_calls: list[dict] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        _ensure_task_runs(self.conn)
        self.conn.execute(
            """
            INSERT INTO task_runs (
                task_run_id, task_id, agent_id, run_id, attempt,
                status, checkpoint_id, artifact_ids, tool_calls,
                started_at, finished_at, error, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_run_id, task_id, agent_id, run_id, attempt,
                status, checkpoint_id,
                json.dumps(artifact_ids or []),
                json.dumps(tool_calls or []),
                (started_at or datetime.now(UTC)).isoformat(),
                finished_at.isoformat() if finished_at else None,
                error,
                json.dumps(metadata or {}),
            ),
        )
        self.conn.commit()

    def update_task_run_status(
        self,
        task_run_id: str,
        status: str,
        checkpoint_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        finished_at = (datetime.now(UTC).isoformat()
                        if status in ("succeeded", "failed", "cancelled") else None)
        cur = self.conn.execute(
            """
            UPDATE task_runs
            SET status = ?, checkpoint_id = COALESCE(?, checkpoint_id),
                error = COALESCE(?, error), finished_at = COALESCE(?, finished_at)
            WHERE task_run_id = ?
            """,
            (status, checkpoint_id, error, finished_at, task_run_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def latest_task_run(self, task_id: str, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id is not None:
            row = self.conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? AND run_id = ? ORDER BY attempt DESC LIMIT 1",
                (task_id, run_id),
            ).fetchone()
            return _row_to_dict(row) if row else None
        row = self.conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY attempt DESC LIMIT 1",
            (task_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_task_runs_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM task_runs WHERE run_id = ? ORDER BY started_at",
            (run_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def resumed_checkpoints(self, run_id: str) -> dict[str, str]:
        """恢复时取出本 run 内每个 task 的最终 checkpoint id（用于 SqliteSaver resume）。

        返回 {task_id: checkpoint_id}
        """
        rows = self.conn.execute(
            """
            SELECT task_id, checkpoint_id FROM task_runs
            WHERE run_id = ? AND checkpoint_id IS NOT NULL
            GROUP BY task_id
            HAVING MAX(attempt)
            """,
            (run_id,)
        ).fetchall()
        return {r["task_id"]: r["checkpoint_id"] for r in rows if r["checkpoint_id"]}

    # ===== TeamEvents =====

    def record_event(
        self,
        event_id: str,
        run_id: str,
        event_type: str,
        agent_id: str | None = None,
        task_id: str | None = None,
        task_run_id: str | None = None,
        trace_id: str | None = None,
        timestamp: datetime | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        occurred_at = (timestamp or datetime.now(UTC)).isoformat()
        conn = self.conn
        _ensure_event_envelopes(conn)
        # Transaction-aware: when a caller already holds a BEGIN IMMEDIATE
        # (e.g. TransactionalTaskService.apply runs mutations inside one and
        # lifecycle hooks call back into record_event), append within that
        # transaction so the event commits atomically with the state change.
        # Starting a nested BEGIN would raise "cannot start a transaction
        # within a transaction".  Only manage BEGIN/COMMIT when no transaction
        # is active.
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            _append_event_envelope(
                conn,
                event_id=event_id,
                run_id=run_id,
                event_type=event_type,
                agent_id=agent_id,
                task_id=task_id,
                task_run_id=task_run_id,
                trace_id=trace_id,
                occurred_at=occurred_at,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            if owns_transaction:
                conn.commit()
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise

    def list_events(self, run_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM team_events WHERE run_id = ?"
        params: list[Any] = [run_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY timestamp ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_event_envelopes(self, run_id: str, after_sequence: int = 0,
                             limit: int = 500) -> list[dict[str, Any]]:
        _ensure_event_envelopes(self.conn)
        rows = self.conn.execute(
            "SELECT * FROM event_envelopes WHERE run_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?", (run_id, after_sequence, min(limit, 2000)),
        ).fetchall()
        result = [_row_to_dict(row) for row in rows]
        for item in result:
            if isinstance(item.get("payload"), dict):
                continue
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["payload"] = {"corrupt_payload": True}
        return result

    def event_envelope_stats(self, run_id: str) -> dict[str, Any]:
        """Return cheap liveness statistics without replaying the audit log."""
        _ensure_event_envelopes(self.conn)
        aggregate = self.conn.execute(
            "SELECT COUNT(*) AS event_count, COALESCE(MAX(sequence), 0) AS last_sequence "
            "FROM event_envelopes WHERE run_id=?",
            (run_id,),
        ).fetchone()
        latest = self.conn.execute(
            "SELECT * FROM event_envelopes WHERE run_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        latest_payload = _row_to_dict(latest) if latest else None
        if latest_payload:
            try:
                latest_payload["payload"] = json.loads(
                    latest_payload.get("payload") or "{}"
                )
            except (TypeError, json.JSONDecodeError):
                latest_payload["payload"] = {"corrupt_payload": True}
        return {
            "event_count": int(aggregate["event_count"]),
            "last_sequence": int(aggregate["last_sequence"]),
            "latest_event": latest_payload,
        }

    # ===== Permission Requests =====

    def insert_permission_request(
        self,
        request_id: str,
        run_id: str,
        agent_id: str,
        operation: str,
        target: str = "",
        reason: str = "",
        created_at: datetime | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO permission_requests (
                request_id, run_id, agent_id, operation, target, reason, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                request_id, run_id, agent_id, operation, target, reason,
                (created_at or datetime.now(UTC)).isoformat()
            ),
        )
        self.conn.commit()

    def decide_permission_request(
        self,
        request_id: str,
        decided_by: str,
        decision: str,
    ) -> bool:
        cur = self.conn.execute(
            """
            UPDATE permission_requests
            SET status = 'decided', decided_by = ?, decision = ?, decided_at = ?
            WHERE request_id = ? AND status = 'pending'
            """,
            (decided_by, decision, datetime.now(UTC).isoformat(), request_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_pending_permission_requests(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            rows = self.conn.execute(
                "SELECT * FROM permission_requests WHERE status = 'pending' AND run_id = ? ORDER BY created_at",
                (run_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM permission_requests WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ===== Artifacts =====

    def insert_artifact(
        self,
        artifact_id: str,
        run_id: str,
        task_id: str,
        type: str,
        relative_path: str,
        content_hash: str,
        size_bytes: int = 0,
        version: int = 1,
        produced_by: str = "",
        status: str = "published",
        predecessor_id: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        supersede_parent_id: str | None = None,
    ) -> None:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # INSERT OR REPLACE would clobber an existing row's ``status``
            # (e.g. ``verified`` / ``rejected``) back to ``published`` on any
            # re-insert with the same artifact_id.  Artifact ids are UUIDs so
            # this is not reachable today, but the REPLACE semantics were a
            # latent footgun: a future refactor that reuses an id would
            # silently un-verify an artifact.  ON CONFLICT DO NOTHING preserves
            # the durable lifecycle state.
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, task_id, type, relative_path, content_hash,
                    size_bytes, version, produced_by, status, predecessor_id,
                    parent_artifact_id, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (
                    artifact_id, run_id, task_id, type, relative_path, content_hash,
                    size_bytes, version, produced_by, status, predecessor_id,
                    parent_artifact_id, datetime.now(UTC).isoformat(),
                    json.dumps(metadata or {})
                )
            )
            if supersede_parent_id:
                updated = conn.execute(
                    "UPDATE artifacts SET status='superseded' "
                    "WHERE artifact_id=? AND run_id=?",
                    (supersede_parent_id, run_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        f"artifact parent not persisted: {supersede_parent_id}"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def list_artifacts_by_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_artifacts_by_task(self, task_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND run_id = ? ORDER BY version",
                (task_id, run_id),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY version", (task_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ===== Mailbox Messages =====

    def insert_mailbox_message(
        self,
        message_id: str,
        from_agent_id: str,
        run_id: str,
        title: str,
        content: str,
        severity: str = "info",
        from_agent_name: str = "",
        from_role: str = "",
        to_agent_id: str | None = None,
        to_role: str | None = None,
        thread_id: str | None = None,
        reply_to: str | None = None,
        delivery_attempts: int = 0,
        consumed_at: datetime | None = None,
        status: str = "delivered",
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO mailbox_messages (
                message_id, from_agent_id, from_agent_name, from_role,
                to_agent_id, to_role, run_id, title, content, severity,
                thread_id, reply_to, delivery_attempts, consumed_at, status,
                created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, from_agent_id, from_agent_name, from_role,
                to_agent_id, to_role, run_id, title, content, severity,
                thread_id, reply_to, delivery_attempts,
                consumed_at.isoformat() if consumed_at else None,
                status,
                (created_at or datetime.now(UTC)).isoformat(),
                json.dumps(metadata or {}),
            )
        )
        self.conn.commit()

    def list_mailbox_messages(
        self,
        run_id: str | None = None,
        to_agent_id: str | None = None,
        thread_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询 mailbox 消息（用于恢复时重新投递 + 审计）。"""
        sql = "SELECT * FROM mailbox_messages WHERE 1=1"
        params: list[Any] = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if to_agent_id is not None:
            sql += " AND to_agent_id = ?"
            params.append(to_agent_id)
        if thread_id is not None:
            sql += " AND thread_id = ?"
            params.append(thread_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def mark_mailbox_consumed(self, message_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE mailbox_messages SET status='consumed', consumed_at=? WHERE message_id=?",
            (datetime.now(UTC).isoformat(), message_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_mailbox_inbox(self, to_agent_id: str) -> int:
        """清除某 agent 的全部未读 inbox（重置场景）。"""
        cur = self.conn.execute(
            "DELETE FROM mailbox_messages WHERE to_agent_id=? AND status='delivered'",
            (to_agent_id,)
        )
        self.conn.commit()
        return cur.rowcount


def _row_to_dict(row: sqlite3_proxy_like) -> dict[str, Any]:
    """row 是 sqlite3.Row；序列化 JSON 字段。"""
    if row is None:
        return {}
    result = {}
    keys = row.keys()
    for k in keys:
        v = row[k]
        if k in ("capabilities", "metadata", "payload", "tool_calls", "artifact_ids") and isinstance(v, str):
            try:
                v = json.loads(v) if v else []
            except json.JSONDecodeError:
                pass
        result[k] = v
    return result


# type alias for hint readability
import sqlite3 as _sqlite3
sqlite3_proxy_like = _sqlite3.Row


def _ensure_task_runs(conn) -> None:
    """在 task_runs 表存在的会话内确保表存在（防御性）。"""
    # 已由 _init_multiagent_db 创建，应已存在
    pass


def _ensure_team_runs(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS team_runs (
            run_id TEXT PRIMARY KEY, goal TEXT NOT NULL, team_id TEXT NOT NULL,
            mode TEXT NOT NULL, workspace_root TEXT NOT NULL, status TEXT NOT NULL,
            max_rounds INTEGER NOT NULL, review_required INTEGER NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    # ``list_team_runs`` orders by ``updated_at DESC LIMIT 50`` on every
    # dashboard load; without this index SQLite does a full table scan plus
    # a filesort on every call.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_runs_updated_at "
        "ON team_runs(updated_at DESC)"
    )
    conn.commit()


def _ensure_task_board_tasks(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_board_tasks (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, task_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_board_tasks_run ON task_board_tasks(run_id)"
    )
    conn.commit()


def _ensure_task_graph_snapshots(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_graph_snapshots (
            run_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            graph_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.commit()


# ===== 全局单例 =====

_history: AgentRunHistory | None = None


def get_agent_run_history() -> AgentRunHistory:
    global _history
    if _history is None:
        _history = AgentRunHistory()
    return _history


def reset_agent_run_history() -> None:
    global _history
    _history = None
    # The per-connection bootstrap cache (_event_envelopes_initialized_conns)
    # keys on id(conn).  Tests (and any caller that closes/reopens the
    # canonical connection) get a fresh connection object whose id() may be
    # reused after the old one is GC'd; a stale cache hit then skips the
    # event_envelopes DDL and the next list_event_envelopes / record_event
    # raises "no such table".  Clearing the cache on reset restores the
    # invariant that a fresh connection always bootstraps its schema.  In
    # production this function is never called, so the cache keeps working.
    _event_envelopes_initialized_conns.clear()
