"""AgentRegistry — 运行时 Agent 注册表。

存储所有 AgentInstance 并支持查询、心跳、租约清理。
Phase C 第一步：纯内存实现 + 能力/状态查询。
Phase C 第二步：持久化（Plan-G 第 7 节）。
"""
from __future__ import annotations

from datetime import datetime
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

    # ===== 注册 =====

    def register_agent(self, instance: AgentInstance) -> AgentInstance:
        """注册一个新 AgentInstance。"""
        instance.heartbeat()
        self._agents[instance.agent_id] = instance
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
            session_id=make_session_id(),
            thread_id=make_session_id(),
            checkpoint_namespace=checkpoint_namespace or f"agent:{name}:{run_id}",
            workspace_root=workspace_root,
            capabilities=capabilities or [],
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
        for a in self._agents.values():
            if a.run_id != run_id:
                continue
            if a.status != AgentStatus.IDLE:
                continue
            if capabilities and not any(c in a.capabilities for c in capabilities):
                continue
            return a
        return None

    # ===== 心跳租约 =====

    def heartbeat(self, agent_id: str) -> bool:
        a = self._agents.get(agent_id)
        if a is None:
            return False
        a.heartbeat()
        return True

    def cleanup_expired(self) -> list[str]:
        """清理租约过期的 Agent。返回被清理的 agent_id 列表。"""
        expiry = []
        now = datetime.utcnow()
        import datetime as dt
        for agent_id, a in list(self._agents.items()):
            if a.last_heartbeat_at is None:
                continue
            idle_time = (now - a.last_heartbeat_at).total_seconds()
            if idle_time > self._lease_timeout and a.is_alive():
                a.update_status(AgentStatus.FAILED)
                expiry.append(agent_id)
                logger.warning(
                    f"[AgentRegistry] agent {agent_id} lease expired ({idle_time:.0f}s)"
                )
        return expiry

    # ===== 销毁 =====

    def stop(self, agent_id: str, reason: str = "") -> bool:
        a = self._agents.get(agent_id)
        if a is None:
            return False
        a.update_status(AgentStatus.STOPPING)
        a.update_status(AgentStatus.STOPPED)
        logger.info(f"[AgentRegistry] stopped agent={agent_id} reason={reason}")
        return True

    def remove(self, agent_id: str) -> bool:
        return self._agents.pop(agent_id, None) is not None


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
