"""多智能体 SQLite 持久化层。

在现有 `app/task/store.py` 的 SQLite 同库下新增表，不破坏单 Agent 旧表：

新增表：
- team_rooms         : room 元信息 + TeamSpec / 配置 JSON + 当前状态 JSON
- team_agents        : room 下的 Agent 定义
- agent_messages     : MessageBus transcript（全量消息）
- agent_inbox        : 每个 Agent 入箱表（含 read 状态）
- team_state_journal : SharedTeamState 变更日志（可选审计）
- team_decisions     : 决策
- team_issues        : issues
- team_rounds        : 每轮记录（speaker / action_summary / termination）

所有方法的最小 CRUD。线程内连接复用现有 task store 的连接。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.multiagent.agent_spec import (
    AgentSpec,
    TeamRunConfig,
    TeamRunConfig as _TeamRunConfig,
    TeamSpec,
)
from app.multiagent.messages import AgentMessage, MessageVisibility, MessageType
from app.multiagent.state import (
    IssueSeverity,
    IssueStatus,
    SharedTeamState,
    TeamArtifactRef,
    TeamDecision,
    TeamIssue,
    TeamPhase,
)


_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = Path(settings.sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _init_multiagent_db(conn)
        _local.conn = conn
    return _local.conn


def _init_multiagent_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS team_rooms (
            room_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            team_name TEXT NOT NULL,
            team_spec_json TEXT NOT NULL DEFAULT '{}',
            config_json TEXT NOT NULL DEFAULT '{}',
            state_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'created',
            assigned_task_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminated INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_team_rooms_task_id ON team_rooms(task_id);
        CREATE INDEX IF NOT EXISTS idx_team_rooms_status ON team_rooms(status);

        CREATE TABLE IF NOT EXISTS team_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            agent_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(room_id, agent_name)
        );
        CREATE INDEX IF NOT EXISTS idx_team_agents_room ON team_agents(room_id);

        CREATE TABLE IF NOT EXISTS agent_messages (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT,
            visibility TEXT NOT NULL DEFAULT 'broadcast',
            message_type TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            cause_by TEXT,
            reply_to TEXT,
            thread_id TEXT,
            requires_response INTEGER NOT NULL DEFAULT 0,
            expected_response_type TEXT,
            evidence TEXT NOT NULL DEFAULT '[]',
            artifact_refs TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_msgs_room ON agent_messages(room_id);
        CREATE INDEX IF NOT EXISTS idx_agent_msgs_task ON agent_messages(task_id);
        CREATE INDEX IF NOT EXISTS idx_agent_msgs_from ON agent_messages(from_agent);
        CREATE INDEX IF NOT EXISTS idx_agent_msgs_type ON agent_messages(message_type);

        CREATE TABLE IF NOT EXISTS agent_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            message_id TEXT NOT NULL,
            from_agent TEXT,
            message_type TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            read_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(room_id, agent_name, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_inbox_room_agent ON agent_inbox(room_id, agent_name);
        CREATE INDEX IF NOT EXISTS idx_agent_inbox_unread ON agent_inbox(is_read);

        CREATE TABLE IF NOT EXISTS team_decisions (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            title TEXT NOT NULL,
            rationale TEXT,
            decided_by TEXT,
            alternatives TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_team_decisions_room ON team_decisions(room_id);

        CREATE TABLE IF NOT EXISTS team_issues (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            severity TEXT,
            status TEXT,
            owner TEXT,
            evidence TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_team_issues_room ON team_issues(room_id);

        CREATE TABLE IF NOT EXISTS team_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            selected_speaker TEXT,
            action_summary TEXT,
            message_ids TEXT NOT NULL DEFAULT '[]',
            termination_reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_team_rounds_room ON team_rounds(room_id);
        """
    )
    conn.commit()


def close_connection() -> None:
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


# ========== 消息转 Pydantic ==========


def _row_to_message(row: sqlite3.Row) -> AgentMessage:
    to_agent_raw = row["to_agent"]
    to_agent: str | list[str] | None
    if to_agent_raw is None:
        to_agent = None
    elif to_agent_raw.startswith("[") and to_agent_raw.endswith("]"):
        to_agent = json.loads(to_agent_raw)
    else:
        to_agent = to_agent_raw
    return AgentMessage(
        id=row["id"],
        task_id=row["task_id"],
        room_id=row["room_id"],
        from_agent=row["from_agent"],
        to_agent=to_agent,
        visibility=MessageVisibility(row["visibility"]),
        message_type=MessageType(row["message_type"]),
        content=row["content"],
        cause_by=row["cause_by"],
        reply_to=row["reply_to"],
        thread_id=row["thread_id"],
        requires_response=bool(row["requires_response"]),
        expected_response_type=row["expected_response_type"],
        evidence=json.loads(row["evidence"] or "[]"),
        artifact_refs=json.loads(row["artifact_refs"] or "[]"),
        metadata=json.loads(row["metadata"] or "{}"),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class MultiAgentStore:
    """多智能体持久化。最小 CRUD + 与 TaskStore 统一展示的桥接。"""

    def __init__(self):
        self.conn = _get_conn()

    # ========== Room ==========

    def save_room(self, room) -> None:
        conn = self.conn
        now = datetime.utcnow().isoformat()
        conn.execute(
            """
            INSERT INTO team_rooms (room_id, task_id, goal, team_name, team_spec_json, config_json,
                                    state_json, status, assigned_task_id, created_at, updated_at, terminated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(room_id) DO UPDATE SET
                goal = excluded.goal,
                team_name = excluded.team_name,
                team_spec_json = excluded.team_spec_json,
                config_json = excluded.config_json,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at,
                terminated = excluded.terminated
            """,
            (
                room.room_id,
                room.task_id,
                room.config.goal,
                room.team_spec.name,
                room.team_spec.model_dump_json(),
                room.config.model_dump_json(),
                room.state.model_dump_json(),
                room.state.phase.value,
                room.task_id,
                now,
                now,
            ),
        )
        conn.commit()

    def load_room(self, room_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM team_rooms WHERE room_id = ?", (room_id,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            team_spec = TeamSpec.model_validate_json(row["team_spec_json"])
        except Exception as exc:
            logger.warning(f"[MultiAgentStore] invalid team_spec: {exc}")
            return None
        try:
            config = TeamRunConfig.model_validate_json(row["config_json"])
        except Exception as exc:
            logger.warning(f"[MultiAgentStore] invalid config: {exc}")
            config = TeamRunConfig(goal=row["goal"] or "", team_name=row["team_name"])
        return {
            "room_id": row["room_id"],
            "task_id": row["task_id"],
            "team_spec": team_spec,
            "config": config,
            "status": row["status"],
            "terminated": bool(row["terminated"]),
        }

    def save_room_meta_timestamp(self, room_id: str, state: SharedTeamState) -> None:
        self.conn.execute(
            "UPDATE team_rooms SET state_json = ?, status = ?, updated_at = ? WHERE room_id = ?",
            (
                state.model_dump_json(),
                state.phase.value,
                datetime.utcnow().isoformat(),
                room_id,
            ),
        )
        self.conn.commit()

    def set_room_terminated(self, room_id: str, terminated: bool, status: str | None = None) -> None:
        self.conn.execute(
            "UPDATE team_rooms SET terminated = ?, status = COALESCE(?, status), updated_at = ? WHERE room_id = ?",
            (
                int(terminated),
                status,
                datetime.utcnow().isoformat(),
                room_id,
            ),
        )
        self.conn.commit()

    def list_rooms(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT room_id, task_id, team_name, status, created_at, updated_at FROM team_rooms ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_room_by_task(self, task_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT room_id FROM team_rooms WHERE task_id = ? ORDER BY updated_at DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self.load_room(row["room_id"])

    # ========== Agents ==========

    def save_agent(self, room_id: str, agent: AgentSpec) -> None:
        self.conn.execute(
            """
            INSERT INTO team_agents (room_id, agent_name, agent_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_id, agent_name) DO UPDATE SET agent_json = excluded.agent_json
            """,
            (room_id, agent.name, agent.model_dump_json(), datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def load_agents(self, room_id: str) -> list[AgentSpec]:
        cur = self.conn.execute(
            "SELECT agent_json FROM team_agents WHERE room_id = ?",
            (room_id,),
        )
        return [AgentSpec.model_validate_json(r["agent_json"]) for r in cur.fetchall()]

    # ========== Messages / Inbox ==========

    def save_message(self, message: AgentMessage) -> None:
        to_agent_json = (
            json.dumps(message.to_agent, ensure_ascii=False)
            if isinstance(message.to_agent, list)
            else (message.to_agent if isinstance(message.to_agent, str) else None)
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO agent_messages
            (id, task_id, room_id, from_agent, to_agent, visibility, message_type, content,
             cause_by, reply_to, thread_id, requires_response, expected_response_type,
             evidence, artifact_refs, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.task_id,
                message.room_id,
                message.from_agent,
                to_agent_json,
                message.visibility.value,
                message.message_type.value,
                message.content,
                message.cause_by,
                message.reply_to,
                message.thread_id,
                int(message.requires_response),
                message.expected_response_type,
                json.dumps(message.evidence, ensure_ascii=False),
                json.dumps(message.artifact_refs, ensure_ascii=False),
                json.dumps(message.metadata, ensure_ascii=False),
                message.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def deliver_to_inbox(
        self,
        agent_name: str,
        message_id: str,
        room_id: str,
        task_id: str,
        from_agent: str | None = None,
        message_type: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO agent_inbox
            (room_id, agent_name, message_id, from_agent, message_type, is_read, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (room_id, agent_name, message_id, from_agent, message_type, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def ack_message(self, message_id: str, agent_name: str) -> None:
        self.conn.execute(
            "UPDATE agent_inbox SET is_read = 1, read_at = ? WHERE message_id = ? AND agent_name = ?",
            (datetime.utcnow().isoformat(), message_id, agent_name),
        )
        self.conn.commit()

    def get_agent_unread_inbox(self, room_id: str, agent_name: str) -> list[AgentMessage]:
        cur = self.conn.execute(
            """
            SELECT m.* FROM agent_inbox i
            JOIN agent_messages m ON i.message_id = m.id
            WHERE i.room_id = ? AND i.agent_name = ? AND i.is_read = 0
            ORDER BY m.created_at ASC
            """,
            (room_id, agent_name),
        )
        return [_row_to_message(r) for r in cur.fetchall()]

    def get_agent_full_inbox(self, room_id: str, agent_name: str) -> list[AgentMessage]:
        cur = self.conn.execute(
            """
            SELECT m.* FROM agent_inbox i
            JOIN agent_messages m ON i.message_id = m.id
            WHERE i.room_id = ? AND i.agent_name = ?
            ORDER BY m.created_at ASC
            """,
            (room_id, agent_name),
        )
        return [_row_to_message(r) for r in cur.fetchall()]

    def get_room_messages(self, room_id: str, limit: int = 200) -> list[AgentMessage]:
        cur = self.conn.execute(
            "SELECT * FROM agent_messages WHERE room_id = ? ORDER BY created_at ASC LIMIT ?",
            (room_id, limit),
        )
        return [_row_to_message(r) for r in cur.fetchall()]

    def get_task_messages(self, task_id: str, limit: int = 200) -> list[AgentMessage]:
        cur = self.conn.execute(
            "SELECT * FROM agent_messages WHERE task_id = ? ORDER BY created_at ASC LIMIT ?",
            (task_id, limit),
        )
        return [_row_to_message(r) for r in cur.fetchall()]

    # ========== State ==========

    def save_state(self, state: SharedTeamState) -> None:
        self.conn.execute(
            "UPDATE team_rooms SET state_json = ?, status = ?, updated_at = ? WHERE room_id = ?",
            (
                state.model_dump_json(),
                state.phase.value,
                datetime.utcnow().isoformat(),
                state.room_id,
            ),
        )
        self.conn.commit()
        # 同步 decisions / issues
        self._sync_decisions(state)
        self._sync_issues(state)

    def _sync_decisions(self, state: SharedTeamState) -> None:
        # 简化：清掉 + 重插
        self.conn.execute("DELETE FROM team_decisions WHERE room_id = ?", (state.room_id,))
        for d in state.decisions:
            self.conn.execute(
                """INSERT OR REPLACE INTO team_decisions
                   (id, room_id, title, rationale, decided_by, alternatives, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    d.id,
                    state.room_id,
                    d.title,
                    d.rationale,
                    d.decided_by,
                    json.dumps(d.alternatives, ensure_ascii=False),
                    d.created_at.isoformat(),
                ),
            )
        self.conn.commit()

    def _sync_issues(self, state: SharedTeamState) -> None:
        self.conn.execute("DELETE FROM team_issues WHERE room_id = ?", (state.room_id,))
        for i in state.issues:
            self.conn.execute(
                """INSERT OR REPLACE INTO team_issues
                   (id, room_id, title, description, severity, status, owner, evidence, created_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    i.id,
                    state.room_id,
                    i.title,
                    i.description,
                    i.severity.value,
                    i.status.value,
                    i.owner,
                    json.dumps(i.evidence, ensure_ascii=False),
                    i.created_at.isoformat(),
                    i.resolved_at.isoformat() if i.resolved_at else None,
                ),
            )
        self.conn.commit()

    def load_state(self, room_id: str) -> SharedTeamState | None:
        cur = self.conn.execute("SELECT state_json FROM team_rooms WHERE room_id = ?", (room_id,))
        row = cur.fetchone()
        if not row or not row["state_json"]:
            return None
        try:
            return SharedTeamState.model_validate_json(row["state_json"])
        except Exception as exc:
            logger.warning(f"[MultiAgentStore] failed to load state: {exc}")
            return None

    # ========== Rounds ==========

    def save_round(
        self,
        room_id: str,
        round_number: int,
        selected_speaker: str,
        action_summary: str = "",
        message_ids: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO team_rounds
               (room_id, round_number, selected_speaker, action_summary, message_ids, termination_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                room_id,
                round_number,
                selected_speaker,
                action_summary,
                json.dumps(message_ids or [], ensure_ascii=False),
                termination_reason,
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()

    def list_rounds(self, room_id: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM team_rounds WHERE room_id = ? ORDER BY round_number ASC",
            (room_id,),
        )
        return [dict(r) for r in cur.fetchall()]


_store: MultiAgentStore | None = None


def get_multiagent_store() -> MultiAgentStore:
    global _store
    if _store is None:
        _store = MultiAgentStore()
    return _store
