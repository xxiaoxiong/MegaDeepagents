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
            langsmith_run_url TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_team_rounds_room ON team_rounds(room_id);

        CREATE TABLE IF NOT EXISTS memory_entries (
            id TEXT PRIMARY KEY,
            tier TEXT NOT NULL,
            agent_scope TEXT,
            content TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            importance REAL NOT NULL DEFAULT 0.5,
            access_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_accessed_at TEXT,
            task_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memory_tier ON memory_entries(tier);
        CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_entries(agent_scope);
        CREATE INDEX IF NOT EXISTS idx_memory_task ON memory_entries(task_id);

        -- Phase G: AgentInstance 持久化
        CREATE TABLE IF NOT EXISTS agent_instances (
            agent_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_namespace TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'created',
            current_task_id TEXT,
            workspace_root TEXT NOT NULL DEFAULT '',
            last_heartbeat_at TEXT,
            capabilities TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stopped_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_inst_run ON agent_instances(run_id);
        CREATE INDEX IF NOT EXISTS idx_agent_inst_status ON agent_instances(status);

        -- Phase G: TaskRun 记录
        CREATE TABLE IF NOT EXISTS task_runs (
            task_run_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'created',
            checkpoint_id TEXT,
            artifact_ids TEXT NOT NULL DEFAULT '[]',
            tool_calls TEXT NOT NULL DEFAULT '[]',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_runs_run ON task_runs(run_id);

        -- Phase G: Artifact 持久化（替代内存注册表）
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'any',
            relative_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            produced_by TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'published',
            predecessor_id TEXT,
            parent_artifact_id TEXT,
            created_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
        CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);

        -- Phase G: Permission Requests
        CREATE TABLE IF NOT EXISTS permission_requests (
            request_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by TEXT,
            decision TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_perm_req_run ON permission_requests(run_id);

        -- Phase G: Team Events 审计日志
        CREATE TABLE IF NOT EXISTS team_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            agent_id TEXT,
            task_id TEXT,
            task_run_id TEXT,
            timestamp TEXT NOT NULL,
            trace_id TEXT,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_team_events_run ON team_events(run_id);
        CREATE INDEX IF NOT EXISTS idx_team_events_type ON team_events(event_type);

        -- Phase G: Mailbox 消息持久化
        CREATE TABLE IF NOT EXISTS mailbox_messages (
            message_id TEXT PRIMARY KEY,
            from_agent_id TEXT NOT NULL,
            from_agent_name TEXT NOT NULL DEFAULT '',
            from_role TEXT NOT NULL DEFAULT '',
            to_agent_id TEXT,           -- NULL = broadcast
            to_role TEXT,
            run_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'info',
            thread_id TEXT,
            reply_to TEXT,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            consumed_at TEXT,
            status TEXT NOT NULL DEFAULT 'delivered',
            created_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_mailbox_run ON mailbox_messages(run_id);
        CREATE INDEX IF NOT EXISTS idx_mailbox_to ON mailbox_messages(to_agent_id);
        CREATE INDEX IF NOT EXISTS idx_mailbox_thread ON mailbox_messages(thread_id);
        CREATE INDEX IF NOT EXISTS idx_mailbox_blocklist ON mailbox_messages(run_id, from_agent_id);

        -- Phase G: Schema Version
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )
    # 检查并更新 schema version
    _ensure_schema_version(conn)

    # 兼容旧库：如果 team_rounds 表已存在但缺 langsmith_run_url 列，则补上
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(team_rounds)").fetchall()}
        if "langsmith_run_url" not in cols:
            conn.execute("ALTER TABLE team_rounds ADD COLUMN langsmith_run_url TEXT")
            logger.info("[store] team_rounds.langsmith_run_url 已补列（兼容旧库）")
    except Exception as exc:
        logger.warning(f"[store] ALTER TABLE team_rounds 失败（可能已存在）：{exc}")
    # 兼容旧库：memory_entries 缺 task_id 列则补上
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_entries)").fetchall()}
        if "task_id" not in cols:
            conn.execute("ALTER TABLE memory_entries ADD COLUMN task_id TEXT")
            logger.info("[store] memory_entries.task_id 已补列（兼容旧库）")
    except Exception as exc:
        logger.warning(f"[store] ALTER TABLE memory_entries 失败（可能已存在）：{exc}")
    conn.commit()


def _ensure_schema_version(conn) -> None:
    """检查并记录当前 schema version。"""
    cur = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current_version = row[0] if row and row[0] else 0
    target_version = 3  # v3: 增加 mailbox_messages 表
    if current_version < target_version:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (target_version, datetime.utcnow().isoformat()),
        )
        logger.info(f"[store] schema version updated: {current_version} → {target_version}")


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
        langsmith_run_url: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO team_rounds
               (room_id, round_number, selected_speaker, action_summary, message_ids,
                termination_reason, langsmith_run_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                room_id,
                round_number,
                selected_speaker,
                action_summary,
                json.dumps(message_ids or [], ensure_ascii=False),
                termination_reason,
                langsmith_run_url,
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

    # ========== Memory Entries（agent 跨任务持久记忆） ==========

    def save_memory_entry(self, entry: dict[str, Any]) -> None:
        """写入或更新一条 memory_entries 记录（按 id UPSERT）。

        entry 字段：id, tier, agent_scope, content, metadata(dict), importance,
                    access_count, created_at(iso), last_accessed_at(iso|None), task_id
        """
        meta_json = json.dumps(entry.get("metadata", {}), ensure_ascii=False)
        self.conn.execute(
            """
            INSERT INTO memory_entries (id, tier, agent_scope, content, metadata, importance,
                                         access_count, created_at, last_accessed_at, task_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tier = excluded.tier,
                agent_scope = excluded.agent_scope,
                content = excluded.content,
                metadata = excluded.metadata,
                importance = excluded.importance,
                access_count = excluded.access_count,
                last_accessed_at = excluded.last_accessed_at,
                task_id = excluded.task_id
            """,
            (
                entry["id"],
                entry["tier"],
                entry.get("agent_scope"),
                entry.get("content", ""),
                meta_json,
                float(entry.get("importance", 0.5)),
                int(entry.get("access_count", 0)),
                entry.get("created_at") or datetime.utcnow().isoformat(),
                entry.get("last_accessed_at"),
                entry.get("task_id"),
            ),
        )
        self.conn.commit()

    def list_memory_entries(
        self,
        tier: str | None = None,
        agent_scope: str | None = None,
        include_shared: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出符合 tier/scope 的记忆条目。include_shared=True 时，agent_scope 过滤会
        同时包含 team-shared（agent_scope IS NULL）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if agent_scope is not None:
            if include_shared:
                clauses.append("(agent_scope = ? OR agent_scope IS NULL)")
                params.append(agent_scope)
            else:
                clauses.append("agent_scope = ?")
                params.append(agent_scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM memory_entries{where} ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("metadata"):
                try:
                    r["metadata"] = json.loads(r["metadata"])
                except (json.JSONDecodeError, TypeError):
                    r["metadata"] = {}
            else:
                r["metadata"] = {}
        return rows

    def search_memory_entries(
        self,
        query: str,
        tier: str | None = None,
        agent_scope: str | None = None,
        include_shared: bool = True,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """关键词模糊检索 memory_entries。返回按 importance DESC 排序的结果。

        检索策略：分词后 AND 命中（每词都必须在 content 中出现，不区分大小写）。
        空查询退化为按 importance 列出。
        """
        all_rows = self.list_memory_entries(
            tier=tier, agent_scope=agent_scope, include_shared=include_shared, limit=1000
        )
        if not query or not query.strip():
            return all_rows[:limit]
        kws = [w.lower() for w in query.split() if w]
        if not kws:
            return all_rows[:limit]
        matched: list[tuple[float, dict[str, Any]]] = []
        for r in all_rows:
            content = (r.get("content") or "").lower()
            hits = sum(1 for kw in kws if kw in content)
            if hits == 0:
                continue
            score = hits + float(r.get("importance", 0.5))
            matched.append((score, r))
        matched.sort(key=lambda t: t[0], reverse=True)
        # bump access_count + last_accessed_at（fire-and-forget，不抛错）
        result_ids = [r["id"] for _, r in matched[:limit]]
        if result_ids:
            now = datetime.utcnow().isoformat()
            placeholders = ",".join("?" * len(result_ids))
            try:
                self.conn.execute(
                    f"UPDATE memory_entries SET access_count = access_count + 1, "
                    f"last_accessed_at = ? WHERE id IN ({placeholders})",
                    (now, *result_ids),
                )
                self.conn.commit()
            except Exception as exc:
                logger.warning(f"[store] bump memory access_count 失败：{exc}")
        return [r for _, r in matched[:limit]]

    def delete_memory_entry(self, entry_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM memory_entries WHERE id = ?", (entry_id,))
        self.conn.commit()
        return cur.rowcount > 0


_store: MultiAgentStore | None = None


def get_multiagent_store() -> MultiAgentStore:
    global _store
    if _store is None:
        _store = MultiAgentStore()
    return _store
