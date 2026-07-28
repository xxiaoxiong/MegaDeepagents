"""AgentRegistry — 运行时 Agent 注册表。

存储所有 AgentInstance 并支持查询、心跳、租约清理。
提供能力/状态查询与持久化租约。
"""
from __future__ import annotations

from datetime import datetime
import threading
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_instance import (
    AgentInstance,
    AgentStatus,
    make_agent_id,
    make_session_id,
)


class AgentRegistry:
    """进程内 AgentInstance 注册表。

    用途：
    - 注册 Agent
    - 按 run_id / team_id / role / status 查询
    - 心跳和租约管理
    - 跨任务持久化（持久化层后面接入）
    """

    def __init__(self, lease_timeout_seconds: int = 60) -> None:
        self._agents: dict[str, AgentInstance] = {}
        self._lease_timeout = lease_timeout_seconds
        self._lock = threading.RLock()

    # ===== 注册 =====

    def register_agent(self, instance: AgentInstance) -> AgentInstance:
        """注册一个新 AgentInstance。"""
        with self._lock:
            instance.heartbeat()
            self._agents[instance.agent_id] = instance
            self._persist(instance)
        logger.info(
            f"[AgentRegistry] registered agent={instance.agent_id} "
            f"role={instance.role} run={instance.run_id}"
        )
        return instance

    def create_agent(
        self,
        profile_id: str,
        name: str,
        role: str,
        team_id: str,
        run_id: str,
        description: str = "",
        capabilities: list[str] | None = None,
        checkpoint_namespace: str | None = None,
        workspace_root: str = "",
        agent_id_override: str | None = None,
        session_id_override: str | None = None,
        thread_id_override: str | None = None,
        checkpoint_namespace_override: str | None = None,
        created_at_override: datetime | None = None,
        max_concurrency: int = 1,
        metadata: dict[str, Any] | None = None,
        worktree_path: str = "",
        mailbox_cursor: int = 0,
    ) -> AgentInstance:
        """快捷创建并注册一个 AgentInstance。"""
        agent_id = agent_id_override or make_agent_id()
        instance = AgentInstance(
            agent_id=agent_id,
            team_id=team_id,
            run_id=run_id,
            profile_id=profile_id,
            name=name,
            role=role,
            description=description,
            session_id=session_id_override or make_session_id(),
            thread_id=thread_id_override or make_session_id(),
            checkpoint_namespace=checkpoint_namespace_override or checkpoint_namespace or f"agent:{name}:{run_id}",
            workspace_root=workspace_root,
            capabilities=capabilities or [],
            created_at=created_at_override or datetime.utcnow(),
            max_concurrency=max_concurrency,
            metadata=metadata or {},
            worktree_path=worktree_path,
            mailbox_cursor=mailbox_cursor,
        )
        # 创建后置 SPAWNING → IDLE
        instance.status = AgentStatus.IDLE
        return self.register_agent(instance)

    # ===== 查询 =====

    def get(self, agent_id: str) -> AgentInstance | None:
        return self._agents.get(agent_id)

    def list_all(self) -> list[AgentInstance]:
        return list(self._agents.values())

    def list_by_run(self, run_id: str) -> list[AgentInstance]:
        return [a for a in self._agents.values() if a.run_id == run_id]

    def list_by_team(self, team_id: str) -> list[AgentInstance]:
        return [a for a in self._agents.values() if a.team_id == team_id]

    def list_by_status(self, status: AgentStatus) -> list[AgentInstance]:
        return [a for a in self._agents.values() if a.status == status]

    def find_by_capability(self, capability: str, run_id: str | None = None) -> list[AgentInstance]:
        result = []
        for a in self._agents.values():
            if capability in a.capabilities:
                if run_id is None or a.run_id == run_id:
                    if a.is_idle() or a.can_work():
                        result.append(a)
        return result

    def find_idle(self, run_id: str, capabilities: list[str] | None = None) -> AgentInstance | None:
        """找一个空闲 Agent（按 capabilities 过滤）。"""
        with self._lock:
            for a in self._agents.values():
                if a.run_id != run_id or a.status != AgentStatus.IDLE:
                    continue
                if capabilities and not set(capabilities).issubset(set(a.capabilities)):
                    continue
                return a
        return None

    def reserve_idle_agent(
        self,
        run_id: str,
        required_capabilities: set[str],
        task_id: str,
        preferred_agent_id: str | None = None,
    ) -> AgentInstance | None:
        """Atomically select and reserve a compatible idle teammate.

        Scheduling used to call ``find_idle`` and only changed the state after
        an await point, so multiple coroutines could select one worker.  The
        CLAIMING state is intentionally set while the same lock is held.
        """
        with self._lock:
            # LLM 偶尔在 task.required_capabilities 里同时声明主角色能力
            # （planning/coding/...）和工具能力（file_write/...) 或混入未知
            # 标签（如 output_artifact_type 的 "config"）。Worker 注册时只声
            # 明主角色能力，导致 subset 永远不成立、调度卡死。这里在主匹配
            # 失败后剥离工具 + 未知能力再试一遍，与 TeamBuilder/Executor 的
            # fallback 一致。
            PRIMARY_CAPS = {
                "planning", "research", "coding", "testing",
                "reviewing", "summarization",
            }
            stripped = {c for c in required_capabilities if c in PRIMARY_CAPS}
            use_caps_list = [required_capabilities]
            if stripped and stripped != required_capabilities:
                use_caps_list.append(stripped)

            agents = list(self._agents.values())
            if preferred_agent_id:
                agents.sort(
                    key=lambda item: item.agent_id != preferred_agent_id
                )
            for use_caps in use_caps_list:
                for agent in agents:
                    if agent.run_id != run_id or agent.status != AgentStatus.IDLE:
                        continue
                    if not use_caps.issubset(set(agent.capabilities)):
                        continue
                    if agent.max_concurrency < 1:
                        continue
                    if not agent.update_status(AgentStatus.CLAIMING):
                        continue
                    agent.current_task_id = task_id
                    agent.heartbeat()
                    self._persist(agent)
                    return agent
        return None

    def release_reservation(self, agent_id: str, task_id: str | None = None) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return False
            if task_id and agent.current_task_id != task_id:
                return False
            if agent.status not in (
                AgentStatus.CLAIMING, AgentStatus.RUNNING, AgentStatus.STOPPING,
            ):
                return False
            # A per-agent stop is cooperative: the executor returns, then
            # this final release is the single place that clears its lease.
            # Never revive a STOPPING teammate back to IDLE.
            if agent.status == AgentStatus.STOPPING:
                agent.update_status(AgentStatus.STOPPED)
            else:
                agent.update_status(AgentStatus.IDLE)
            agent.current_task_id = None
            agent.heartbeat()
            self._persist(agent)
            return True

    def transition(self, agent_id: str, status: AgentStatus) -> bool:
        """Apply and persist a lifecycle transition owned by the registry."""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None or not agent.update_status(status):
                return False
            self._persist(agent)
            return True

    # ===== 心跳租约 =====

    def heartbeat(self, agent_id: str) -> bool:
        a = self._agents.get(agent_id)
        if a is None:
            return False
        a.heartbeat()
        self._persist(a)
        return True

    def cleanup_expired(self) -> list[str]:
        """Reap agents whose lease has expired while in an active state.

        Only agents that are CLAIMING/RUNNING/STOPPING (i.e. actively doing
        work or mid-transition) are reaped.  IDLE agents are intentionally
        left alone: they are not consuming a task and the scheduler's
        run-wide heartbeat is responsible for keeping their lease fresh.
        Reaping IDLE agents was the historical cause of whole-team death
        during long-running tasks — every idle teammate got marked FAILED
        the moment a sibling task ran past ``lease_timeout``.
        """
        from app.multiagent.agent_instance import AgentStatus
        expiry = []
        now = datetime.utcnow()
        active_states = {
            AgentStatus.CLAIMING, AgentStatus.RUNNING, AgentStatus.STOPPING,
        }
        for agent_id, a in list(self._agents.items()):
            if a.last_heartbeat_at is None:
                continue
            if a.status not in active_states:
                # IDLE/FAILED/STOPPED/BLOCKED agents are never reaped here.
                continue
            idle_time = (now - a.last_heartbeat_at).total_seconds()
            if idle_time > self._lease_timeout and a.is_alive():
                a.update_status(AgentStatus.FAILED)
                self._persist(a)
                expiry.append(agent_id)
                logger.warning(
                    f"[AgentRegistry] agent {agent_id} lease expired ({idle_time:.0f}s) "
                    f"while status={a.status.value}"
                )
        return expiry

    # ===== 销毁 =====

    def stop(self, agent_id: str, reason: str = "") -> bool:
        with self._lock:
            a = self._agents.get(agent_id)
            if a is None or a.status == AgentStatus.STOPPED:
                return False
            if a.status in (AgentStatus.CLAIMING, AgentStatus.RUNNING):
                # The scheduler will finalize this after its executor returns.
                if not a.update_status(AgentStatus.STOPPING):
                    return False
            else:
                if not a.update_status(AgentStatus.STOPPING):
                    return False
                if not a.update_status(AgentStatus.STOPPED):
                    return False
            self._persist(a)
        logger.info(f"[AgentRegistry] stopped agent={agent_id} reason={reason}")
        return True

    def remove(self, agent_id: str) -> bool:
        return self._agents.pop(agent_id, None) is not None

    @staticmethod
    def _persist(agent: AgentInstance) -> None:
        """Make registry lifecycle mutations durable at their source.

        Callers must not need to remember a second persistence operation after
        every reserve/release/heartbeat transition; a restart should observe
        the same agent lease that the scheduler just made.
        """
        try:
            from app.infrastructure.database.run_store import get_agent_run_history
            get_agent_run_history().upsert_agent_instance(
                agent_id=agent.agent_id, team_id=agent.team_id, run_id=agent.run_id,
                profile_id=agent.profile_id, name=agent.name, role=agent.role,
                session_id=agent.session_id, thread_id=agent.thread_id,
                checkpoint_namespace=agent.checkpoint_namespace,
                status=agent.status.value, current_task_id=agent.current_task_id,
                workspace_root=agent.workspace_root,
                last_heartbeat_at=agent.last_heartbeat_at,
                capabilities=agent.capabilities,
                metadata={**agent.metadata, "worktree_path": agent.worktree_path,
                          "mailbox_cursor": agent.mailbox_cursor},
                created_at=agent.created_at, stopped_at=agent.stopped_at,
            )
        except Exception as exc:
            # Scheduling must observe the transition even if durable storage is
            # temporarily unavailable; the run will fail/recover explicitly,
            # never silently become completed.
            logger.error("[AgentRegistry] persist agent=%s failed: %s", agent.agent_id, exc)


# ===== 全局单例 =====

_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


def reset_agent_registry() -> None:
    global _registry
    _registry = None
