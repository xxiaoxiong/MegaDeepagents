"""Build the real, persistent teammates for a TASK_TEAM run."""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.multiagent.agent_instance import AgentInstance
from app.multiagent.agent_profile import get_capability_registry
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.default_teams import get_team
from app.multiagent.mailbox import get_mailbox
from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id
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
            profile = profiles.find_best_worker(set(node.required_capabilities))
            if profile is None:
                # Do not disguise a missing worker as a privileged coder.
                raise RuntimeError(
                    f"no_matching_worker for task={node.id} capabilities={node.required_capabilities}"
                )
            required_profile_ids.add(profile.id)

        # Verification and final hand-off are control-plane roles.  They are
        # added only as needed to reach a viable 3-person minimum, never by
        # blindly spawning every template member.
        if len(required_profile_ids) < 3:
            required_profile_ids.update({"reviewer", "finalizer"})
        if len(required_profile_ids) < 3:
            required_profile_ids.add("planner")
        selected = [p for p in profiles.list_profiles() if p.id in required_profile_ids][:5]
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
            history.record_event(
                event_id=make_run_event_id(), run_id=ctx.run_id, event_type="agent_spawned",
                agent_id=agent.agent_id,
                payload={"profile_id": profile.id, "session_id": agent.session_id,
                         "thread_id": agent.thread_id},
            )
            created.append(agent)
        logger.info("[TeamBuilder] run=%s spawned=%s", ctx.run_id, len(created))
        return created
