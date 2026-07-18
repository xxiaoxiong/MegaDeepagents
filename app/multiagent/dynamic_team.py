"""Budgeted dynamic teammate creation with parent/child permission limits."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.agent_profile import AgentProfile, get_capability_registry
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id
from app.multiagent.store import _get_conn
from app.multiagent.teammate_session import get_teammate_supervisor


class TeamBudget(BaseModel):
    max_team_size: int = Field(default=8, ge=1)
    max_spawn_depth: int = Field(default=2, ge=0)
    max_agents_per_run: int = Field(default=12, ge=1)
    max_concurrency: int = Field(default=4, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost: float | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=1)


class SpawnPolicy(BaseModel):
    allow_teammate_spawn: bool = True
    allow_nested_spawn: bool = True
    require_permission: bool = True


class ParentChildAgentLink(BaseModel):
    run_id: str
    parent_agent_id: str
    child_agent_id: str
    depth: int
    created_at: datetime = Field(default_factory=datetime.utcnow)


def _ensure_schema() -> None:
    conn = _get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS parent_child_agent_links (
            child_agent_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            parent_agent_id TEXT NOT NULL, depth INTEGER NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    conn.commit()


class DynamicTeamManager:
    def __init__(
        self, registry: AgentRegistry | None = None,
        budget: TeamBudget | None = None, policy: SpawnPolicy | None = None,
    ) -> None:
        self.registry = registry or get_agent_registry()
        self.budget = budget or TeamBudget()
        self.policy = policy or SpawnPolicy()
        _ensure_schema()

    def spawn(
        self, *, run_id: str, team_id: str, required_capabilities: set[str],
        requested_by: str, parent_agent_id: str | None = None,
    ) -> Any:
        agents = self.registry.list_by_run(run_id)
        if len(agents) >= min(self.budget.max_team_size, self.budget.max_agents_per_run):
            raise RuntimeError("team_budget:max_team_size")
        depth = 0
        parent = None
        if parent_agent_id:
            if not self.policy.allow_nested_spawn:
                raise PermissionError("nested spawn disabled")
            parent = self.registry.get(parent_agent_id)
            if parent is None or parent.run_id != run_id:
                raise ValueError("parent agent is not in run")
            link = self.link_for(parent_agent_id)
            depth = (link.depth if link else 0) + 1
            if depth > self.budget.max_spawn_depth:
                raise RuntimeError("team_budget:max_spawn_depth")
        elif not self.policy.allow_teammate_spawn:
            raise PermissionError("teammate spawn disabled")

        profile = get_capability_registry().find_best_worker(required_capabilities)
        if profile is None:
            raise RuntimeError(f"no_matching_profile:{sorted(required_capabilities)}")
        if parent is not None:
            parent_profile = get_capability_registry().get_profile(parent.profile_id)
            if parent_profile is None:
                raise RuntimeError("parent profile missing")
            profile = self._restrict_to_parent(profile, parent_profile, parent.agent_id)

        agent = self.registry.create_agent(
            profile_id=profile.id, name=profile.name, role=profile.role,
            team_id=team_id, run_id=run_id, description=profile.description,
            capabilities=sorted(profile.capabilities),
            checkpoint_namespace=f"team:{run_id}:{profile.id}:{len(agents) + 1}",
            workspace_root=parent.workspace_root if parent else (agents[0].workspace_root if agents else ""),
            max_concurrency=min(profile.max_concurrency, self.budget.max_concurrency),
        )
        get_teammate_supervisor().ensure_session(agent)
        if parent_agent_id:
            self._save_link(ParentChildAgentLink(
                run_id=run_id, parent_agent_id=parent_agent_id,
                child_agent_id=agent.agent_id, depth=depth,
            ))
        hook = get_lifecycle_hook_engine().emit(
            LifecycleEvent.TEAMMATE_SPAWNED,
            {"run_id": run_id, "agent_id": agent.agent_id,
             "requested_by": requested_by, "parent_agent_id": parent_agent_id},
        )
        if hook.block:
            self.registry.stop(agent.agent_id, hook.feedback or "spawn hook blocked")
            raise PermissionError(hook.feedback)
        get_agent_run_history().record_event(
            event_id=make_run_event_id(), run_id=run_id,
            event_type="TeammateSpawned", agent_id=agent.agent_id,
            payload={"requested_by": requested_by, "parent_agent_id": parent_agent_id,
                     "depth": depth, "profile_id": profile.id},
        )
        return agent

    def link_for(self, child_agent_id: str) -> ParentChildAgentLink | None:
        row = _get_conn().execute(
            "SELECT payload FROM parent_child_agent_links WHERE child_agent_id=?",
            (child_agent_id,),
        ).fetchone()
        return ParentChildAgentLink.model_validate(json.loads(row["payload"])) if row else None

    @staticmethod
    def _restrict_to_parent(profile: AgentProfile, parent: AgentProfile,
                            parent_agent_id: str) -> AgentProfile:
        child_tools = [tool for tool in profile.tool_policy.allowed_tools
                       if tool in parent.tool_policy.allowed_tools]
        child_policy = profile.tool_policy.model_copy(update={
            "allowed_tools": child_tools,
            "allow_file_read": profile.tool_policy.allow_file_read and parent.tool_policy.allow_file_read,
            "allow_file_write": profile.tool_policy.allow_file_write and parent.tool_policy.allow_file_write,
            "allow_shell": profile.tool_policy.allow_shell and parent.tool_policy.allow_shell,
            "deny_all_by_default": True,
        })
        adapted = profile.model_copy(deep=True, update={
            "id": f"{profile.id}:child:{parent_agent_id}",
            "tool_policy": child_policy,
            "metadata": {**profile.metadata, "permission_parent_profile": parent.id},
        })
        get_capability_registry().register(adapted)
        return adapted

    @staticmethod
    def _save_link(link: ParentChildAgentLink) -> None:
        _get_conn().execute(
            "INSERT OR REPLACE INTO parent_child_agent_links VALUES (?, ?, ?, ?, ?, ?)",
            (link.child_agent_id, link.run_id, link.parent_agent_id, link.depth,
             json.dumps(link.model_dump(mode="json")), link.created_at.isoformat()),
        )
        _get_conn().commit()
