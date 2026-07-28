"""Build the real, persistent teammates for a TASK_TEAM run."""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.multiagent.agent_instance import AgentInstance
from app.multiagent.agent_profile import get_capability_registry
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.default_teams import get_team
from app.multiagent.mailbox import get_mailbox
from app.infrastructure.database.run_store import get_agent_run_history, make_run_event_id
from app.multiagent.team_run_context import TeamRunContext


class TeamBuilder:
    """Convert a TeamSpec and the planned graph into active teammates.

    This is deliberately the only normal-run spawn point.  Resume restores
    prior instances instead of creating lookalikes with fresh sessions.
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self.registry = registry or get_agent_registry()

    async def build_team(self, ctx: TeamRunContext, team_spec: Any, task_graph: Any) -> list[AgentInstance]:
        return self.build_team_sync(ctx, team_spec, task_graph)

    def build_team_sync(self, ctx: TeamRunContext, team_spec: Any, task_graph: Any) -> list[AgentInstance]:
        existing = self.registry.list_by_run(ctx.run_id)
        if existing:
            return existing
        team_spec = team_spec or get_team(ctx.team_id)
        if team_spec is None:
            raise ValueError(f"unknown team: {ctx.team_id}")

        profiles = get_capability_registry()
        required_profile_ids: set[str] = set()
        for node in task_graph.nodes.values():
            caps = set(node.required_capabilities)
            profile = profiles.find_best_worker(caps)
            if profile is None:
                # Fallback: LLM 偶尔会在 task 的 required_capabilities 里附带
                # 工具能力（file_read/file_write/shell_execute/web_research/
                # mcp_access），而该主角色 Worker 并不声明这些工具，导致
                # find_workers 取交集后无人可匹配。这里先尝试去掉所有工具
                # 能力，只按主角色重新匹配，让任务仍可被调度而非整 run failed。
                TOOL_CAPS = {
                    "file_read", "file_write", "shell_execute",
                    "web_research", "mcp_access", "default",
                }
                PRIMARY_CAPS = {
                    "planning", "research", "coding", "testing",
                    "reviewing", "summarization",
                }
                # 二次降级：剥离工具能力 + 未知能力标签（如 LLM 误把
                # output_artifact_type 的 "config" 当作能力声明），仅保留主角色。
                stripped = {c for c in caps if c in PRIMARY_CAPS}
                if stripped and stripped != caps:
                    profile = profiles.find_best_worker(stripped)
                    logger.warning(
                        f"[TeamBuilder] task={node.id} 原始能力{caps}无匹配 Worker，"
                        f"剥离非主角色能力后以{stripped}重新匹配到"
                        f"profile={profile.id if profile else None}"
                    )
                elif stripped != caps:
                    # 只有工具/未知能力，没有任何主角色 —— 走原始剥离路径
                    stripped_tools = {c for c in caps if c not in TOOL_CAPS}
                    if stripped_tools and stripped_tools != caps:
                        profile = profiles.find_best_worker(stripped_tools)
                        logger.warning(
                            f"[TeamBuilder] task={node.id} 原始能力{caps}无匹配 Worker，"
                            f"剥离工具能力后以{stripped_tools}重新匹配到"
                            f"profile={profile.id if profile else None}"
                        )
            if profile is None:
                raise RuntimeError(
                    f"no_matching_worker for task={node.id} capabilities={node.required_capabilities}"
                )
            required_profile_ids.add(profile.id)

        # Team size follows actual graph capability demand.  The
        # ``required_profile_ids`` filter is the correct demand-based limiter:
        # only profiles needed by at least one task are spawned.  A hard
        # ``[:5]`` cap here used to silently drop the Finalizer (the 6th
        # registered profile) when a plan required both Researcher and
        # Finalizer, deadlocking every ``summarization`` task (T14/T15 in
        # run_2a438328372441d8) with ``no_eligible_worker``.
        selected = [p for p in profiles.list_profiles() if p.id in required_profile_ids]
        if not selected:
            raise RuntimeError("no_executable_teammates")

        history = get_agent_run_history()
        mailbox = get_mailbox()
        created: list[AgentInstance] = []
        for profile in selected:
            agent = self.registry.create_agent(
                profile_id=profile.id, name=profile.name, role=profile.role,
                team_id=ctx.team_id, run_id=ctx.run_id,
                description=profile.description, capabilities=sorted(profile.capabilities),
                checkpoint_namespace=f"{ctx.checkpoint_namespace}:{profile.id}",
                workspace_root=ctx.workspace_root, max_concurrency=profile.max_concurrency,
            )
            # Force creation of a dedicated inbox; Mailbox owns no shared
            # implicit "None" inbox for teammates.
            mailbox._inboxes[agent.agent_id]
            from app.multiagent.teammate_session import get_teammate_supervisor
            get_teammate_supervisor().ensure_session(agent)
            history.record_event(
                event_id=make_run_event_id(), run_id=ctx.run_id, event_type="agent_spawned",
                agent_id=agent.agent_id,
                payload={"profile_id": profile.id, "session_id": agent.session_id,
                         "thread_id": agent.thread_id},
            )
            created.append(agent)
        logger.info("[TeamBuilder] run=%s spawned=%s", ctx.run_id, len(created))
        return created
