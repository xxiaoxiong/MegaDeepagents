"""Build the real, persistent teammates for a TASK_TEAM run."""
from __future__ import annotations

from collections import Counter
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_instance import AgentInstance
from app.multiagent.agent_profile import get_capability_registry
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.default_teams import get_team
from app.multiagent.mailbox import get_mailbox
from app.infrastructure.database.run_store import get_agent_run_history, make_run_event_id
from app.multiagent.team_run_context import TeamRunContext


# 同一 profile 最多生成的并行 agent 数量上限。超过此数的并行任务由
# 既有 agent 通过队列轮转处理，避免为单个 run 生成过多 agent 导致
# 内存/checkpoint 压力。
_MAX_PARALLEL_AGENTS_PER_PROFILE = 3


class TeamBuilder:
    """Convert a TeamSpec and the planned graph into active teammates.

    This is deliberately the only normal-run spawn point.  Resume restores
    prior instances instead of creating lookalikes with fresh sessions.
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self.registry = registry or get_agent_registry()

    async def build_team(self, ctx: TeamRunContext, team_spec: Any, task_graph: Any) -> list[AgentInstance]:
        return self.build_team_sync(ctx, team_spec, task_graph)

    @staticmethod
    def _compute_task_depths(nodes: dict[str, Any]) -> dict[str, int]:
        """Compute the dependency depth of each task node.

        Depth = longest path from any root (a root has no dependencies).
        Two tasks at the same depth are guaranteed **not** to depend on each
        other (if A→B then depth(A) > depth(B)), so they can run in parallel.
        Used to determine how many agents of the same profile to spawn.
        """
        depths: dict[str, int] = {}

        def depth_of(node_id: str) -> int:
            if node_id in depths:
                return depths[node_id]
            node = nodes.get(node_id)
            if node is None:
                return 0
            deps = getattr(node, "dependencies", None) or []
            if not deps:
                depths[node_id] = 0
            else:
                depths[node_id] = 1 + max(
                    (depth_of(d) for d in deps if d in nodes), default=0
                )
            return depths[node_id]

        for nid in nodes:
            depth_of(nid)
        return depths

    def build_team_sync(self, ctx: TeamRunContext, team_spec: Any, task_graph: Any) -> list[AgentInstance]:
        existing = self.registry.list_by_run(ctx.run_id)
        team_spec = team_spec or get_team(ctx.team_id)
        if team_spec is None:
            raise ValueError(f"unknown team: {ctx.team_id}")

        profiles = get_capability_registry()
        allowed_roles = {
            str(agent.role).strip().lower()
            for agent in team_spec.agents
        }

        def find_allowed(required: set[str]):
            candidates = [
                profile
                for profile in profiles.find_workers(required)
                if profile.role.strip().lower() in allowed_roles
            ]
            if not candidates:
                return None
            return max(
                candidates,
                key=lambda profile: (profiles.score_worker(profile), profile.id),
            )

        # ---- 第一步：为每个 task 匹配 profile，并记录 (task_id, profile_id) ----
        task_profile_map: dict[str, str] = {}
        required_profile_ids: set[str] = set()
        for node in task_graph.nodes.values():
            caps = set(node.required_capabilities)
            profile = find_allowed(caps)
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
                    profile = find_allowed(stripped)
                    logger.warning(
                        f"[TeamBuilder] task={node.id} 原始能力{caps}无匹配 Worker，"
                        f"剥离非主角色能力后以{stripped}重新匹配到"
                        f"profile={profile.id if profile else None}"
                    )
                elif stripped != caps:
                    # 只有工具/未知能力，没有任何主角色 —— 走原始剥离路径
                    stripped_tools = {c for c in caps if c not in TOOL_CAPS}
                    if stripped_tools and stripped_tools != caps:
                        profile = find_allowed(stripped_tools)
                        logger.warning(
                            f"[TeamBuilder] task={node.id} 原始能力{caps}无匹配 Worker，"
                            f"剥离工具能力后以{stripped_tools}重新匹配到"
                            f"profile={profile.id if profile else None}"
                        )
            if profile is None:
                raise RuntimeError(
                    f"no_matching_worker for task={node.id} capabilities={node.required_capabilities}"
                )
            task_profile_map[node.id] = profile.id
            required_profile_ids.add(profile.id)

        # ---- 第二步：计算并行需求 ----
        #
        # 同一 profile 的多个任务如果处于同一依赖深度层，它们互不依赖、可
        # 并行执行（run_de866d4e976b4c3a：task_2/task_3 均为 coding、均只
        # 依赖 task_1，可并行；但旧代码只生成 1 个 coder → 串行执行 → 用户
        # 感觉"只有 planner 在干活"）。为每个 profile 取其在同一深度层的
        # 最大任务数作为并行 agent 数（cap 在 _MAX_PARALLEL_AGENTS_PER_PROFILE）。
        depths = self._compute_task_depths(task_graph.nodes)
        # (profile_id, depth) → task count
        profile_depth_demand: Counter[tuple[str, int]] = Counter()
        for task_id, profile_id in task_profile_map.items():
            d = depths.get(task_id, 0)
            profile_depth_demand[(profile_id, d)] += 1

        # profile_id → 需要的 agent 数量（同一深度的最大任务数，至少 1）
        profile_agent_demand: Counter[str] = Counter()
        for (profile_id, _depth), count in profile_depth_demand.items():
            profile_agent_demand[profile_id] = max(
                profile_agent_demand.get(profile_id, 0), count
            )
        # 确保每个被需要的 profile 至少 1 个 agent
        for pid in required_profile_ids:
            if profile_agent_demand[pid] < 1:
                profile_agent_demand[pid] = 1
            profile_agent_demand[pid] = min(
                profile_agent_demand[pid], _MAX_PARALLEL_AGENTS_PER_PROFILE
            )

        # ---- 第三步：按需求生成 agent（支持同 profile 多实例）----
        #
        # 旧代码用 set 去重，导致同一 profile 永远只有 1 个 agent。现在按
        # profile_agent_demand 生成 N 个实例。已存活（非 stopped/failed）的
        # 同 profile agent 计入已有数量，只补差值。
        live_profile_counts: Counter[str] = Counter()
        for agent in existing:
            status = getattr(agent.status, "value", agent.status)
            if status not in {"stopped", "failed"}:
                live_profile_counts[agent.profile_id] += 1

        # 构建 selected_profiles 列表（同一 profile 可出现多次）
        selected_profiles: list[Any] = []
        for profile in profiles.list_profiles():
            if profile.id not in required_profile_ids:
                continue
            needed = profile_agent_demand.get(profile.id, 1)
            already = live_profile_counts.get(profile.id, 0)
            to_spawn = max(0, needed - already)
            for _ in range(to_spawn):
                selected_profiles.append(profile)

        if not selected_profiles and required_profile_ids:
            # 所有需求已被现有 agent 满足
            return existing
        if not selected_profiles:
            raise RuntimeError("no_executable_teammates")

        history = get_agent_run_history()
        mailbox = get_mailbox()
        created: list[AgentInstance] = []
        # 同 profile 多实例时给 name 加序号，便于前端区分
        spawn_index: Counter[str] = Counter()
        for profile in selected_profiles:
            spawn_index[profile.id] += 1
            idx = spawn_index[profile.id]
            agent_name = profile.name if idx == 1 and live_profile_counts[profile.id] == 0 else f"{profile.name}-{idx}"
            agent = self.registry.create_agent(
                profile_id=profile.id, name=agent_name, role=profile.role,
                team_id=ctx.team_id, run_id=ctx.run_id,
                description=profile.description, capabilities=sorted(profile.capabilities),
                checkpoint_namespace=f"{ctx.checkpoint_namespace}:{profile.id}:{idx}",
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
        logger.info(
            "[TeamBuilder] run=%s spawned=%s profile_demand=%s",
            ctx.run_id, len(created), dict(profile_agent_demand),
        )
        return [*existing, *created]
