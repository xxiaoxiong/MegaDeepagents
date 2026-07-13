"""Phase G 持久化层：AgentInstance / TaskRun / Artifact / PermissionRequest / TeamEvent。

设计：
- 复用 store.py 的 SQLite 连接（线程本地），不引入新库
- 与 MultiAgentStore 并存（避免兼容旧表回归）
- 所有写入均带 created_at/updated_at；
- 提供 resume_run / load_completed_tasks 等用于恢复（Phase G 第 1 步）
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.store import _get_conn


def make_run_event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:12]


def make_permission_request_id() -> str:
    return "preq_" + uuid.uuid4().hex[:12]


def make_task_run_id() -> str:
    return "trun_" + uuid.uuid4().hex[:12]


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
        now = datetime.utcnow().isoformat()
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
                                (status, datetime.utcnow().isoformat(), run_id))
        self.conn.commit()
        return cur.rowcount > 0

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
        now = datetime.utcnow().isoformat()
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
                (created_at or datetime.utcnow()).isoformat(),
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
                (started_at or datetime.utcnow()).isoformat(),
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
        finished_at = (datetime.utcnow().isoformat()
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
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO team_events (
                event_id, run_id, event_type, agent_id, task_id, task_run_id,
                timestamp, trace_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, run_id, event_type, agent_id, task_id, task_run_id,
                (timestamp or datetime.utcnow()).isoformat(),
                trace_id, json.dumps(payload or {}),
            ),
        )
        self.conn.commit()

    def list_events(self, run_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM team_events WHERE run_id = ?"
        params: list[Any] = [run_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY timestamp ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

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
                (created_at or datetime.utcnow()).isoformat()
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
            (decided_by, decision, datetime.utcnow().isoformat(), request_id)
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
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO artifacts (
                artifact_id, run_id, task_id, type, relative_path, content_hash,
                size_bytes, version, produced_by, status, predecessor_id,
                parent_artifact_id, created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id, run_id, task_id, type, relative_path, content_hash,
                size_bytes, version, produced_by, status, predecessor_id,
                parent_artifact_id, datetime.utcnow().isoformat(),
                json.dumps(metadata or {})
            )
        )
        self.conn.commit()

    def list_artifacts_by_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

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
                (created_at or datetime.utcnow()).isoformat(),
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
            (datetime.utcnow().isoformat(), message_id)
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
