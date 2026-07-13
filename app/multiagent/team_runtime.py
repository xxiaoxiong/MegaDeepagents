"""TeamRuntimeFacade — 统一团队运行入口（Phase A+B 门户）。

设计目的：
- CLI / API / Web 三路入口不再各自拼装 run_id / workspace / artifact_store，
  统一通过 TeamRuntimeFacade 创建 Run、选择 runtime、监控终止。
- 根据 TeamRunContext.mode 自动路由到：
    * TASK_TEAM（默认） → Phase Two TaskGraph-based Orchestrator
    * DISCUSSION        → 传统 TeamRunner（多 Agent 群聊）
- 与 TeamRunner 的关系：DISCUSSION 模式在 _run_discussion 内创建
  TeamRunner；TeamRuntimeFacade 不是 TeamRunner 的替代，而是其上层门户。

全局单例 get_team_runtime() 供顶层路由（CLI / API / Web）使用。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import TeamRunResult
from app.multiagent.team_run_context import TeamRunContext, TeamRunMode


class TeamRuntimeFacade:
    """Unified entry point for all team runs.

    CLI, API and Web routes must ONLY go through this facade.
    """

    def __init__(self):
        self._active_runs: dict[str, dict[str, Any]] = {}

    async def create_run(
        self,
        goal: str,
        team_name: str = "software_dev_team",
        mode: TeamRunMode = TeamRunMode.TASK_TEAM,
        max_rounds: int = 20,
        review_required: bool = True,
        workspace_root: str | None = None,
        user_id: str | None = None,
    ) -> TeamRunContext:
        """Create a new team run.

        创建一个 Run 只分配身份/路径/仓储引用，不产生 LLM 调用。
        实际执行需要调用 start_run()。
        """
        import os
        from pathlib import Path

        run_id = "run_" + uuid.uuid4().hex[:16]
        team_id = team_name

        if not workspace_root:
            workspace_root = str(
                Path(os.getcwd()) / "runtime" / "workspaces" / run_id
            )

        ctx = TeamRunContext(
            run_id=run_id,
            team_id=team_id,
            mode=mode,
            workspace_root=workspace_root,
            checkpoint_namespace=f"team:{run_id}",
            user_id=user_id,
        )

        os.makedirs(ctx.workspace_root, exist_ok=True)
        os.makedirs(ctx.artifacts_dir(), exist_ok=True)

        logger.info(
            f"[TeamRuntimeFacade] Run created: {run_id} team={team_id} "
            f"mode={mode.value} workspace={workspace_root}"
        )
        return ctx

    async def start_run(
        self,
        ctx: TeamRunContext,
        goal: str,
        team_name: str = "software_dev_team",
        max_rounds: int = 20,
        review_required: bool = True,
    ) -> TeamRunResult:
        """Start a team run using the appropriate runtime.

        根据 ctx.mode 自动路由：
        - TASK_TEAM → Phase Two task-graph orchestrator
        - DISCUSSION → Legacy TeamRunner
        """
        if ctx.mode == TeamRunMode.DISCUSSION:
            return await self._run_discussion(
                ctx, goal, team_name, max_rounds, review_required
            )
        else:
            return await self._run_task_team(
                ctx, goal, team_name, max_rounds, review_required
            )

    # ===== TASK_TEAM mode =====

    async def _run_task_team(
        self,
        ctx: TeamRunContext,
        goal: str,
        team_name: str,
        max_rounds: int,
        review_required: bool,
    ) -> TeamRunResult:
        """Run using the Phase Two task-graph based orchestrator."""
        from app.multiagent.artifact import ArtifactStore
        from app.multiagent.executor import DeepAgentExecutor
        from app.multiagent.orchestrator import run_orchestrated
        from app.multiagent.planner import plan_with_llm
        from app.multiagent.verifier import LLMRubricVerifier, Verifier

        # Create ArtifactStore for this run
        artifact_store = ArtifactStore(root_path=ctx.artifacts_dir())

        # Create executor with workspace
        executor = DeepAgentExecutor(workspace_root=ctx.workspace_root)
        executor._artifact_store = artifact_store  # Inject artifact store

        # Verifier that reads real files
        verifier = Verifier(
            llm_rubric=LLMRubricVerifier(model_available=False),
        )

        result = run_orchestrated(
            goal=goal,
            mode_override="full_multi",
            planner=lambda g, c: plan_with_llm(g, context=c),
            executor=executor,
            verifier=verifier,
            ctx=ctx,  # Pass context
        )

        # Map to TeamRunResult
        status_map = {
            "completed": "completed",
            "failed": "failed",
            "interrupted": "cancelled",
            "incomplete": "failed",
        }
        return TeamRunResult(
            task_id=ctx.run_id,
            status=status_map.get(result.status, "failed"),
            final_output=result.summary,
            total_rounds=result.rounds,
            termination_reason=result.error,
            completed_at=datetime.utcnow(),
        )

    # ===== DISCUSSION mode =====

    async def _run_discussion(
        self,
        ctx: TeamRunContext,
        goal: str,
        team_name: str,
        max_rounds: int,
        review_required: bool,
    ) -> TeamRunResult:
        """Run using the legacy TeamRunner (discussion mode)."""
        from app.multiagent.team_runner import TeamRunner, _run_team_traced

        runner = TeamRunner.create(
            goal=goal,
            team_name=team_name,
            max_rounds=max_rounds,
            review_required=review_required,
            task_id=ctx.run_id,
        )
        runner.room_id = ctx.run_id  # Override to use run_id

        # Run synchronously via the traced wrapper
        result = _run_team_traced(runner)
        return result

    # ===== Run 生命周期管理 =====

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel an active run. 返回 False 表示该 run_id 不存在或已结束。"""
        run = self._active_runs.get(run_id)
        if not run:
            return False
        # TODO: implement proper cancellation via asyncio task handle
        return True

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """查询活跃 Run 的元信息。"""
        return self._active_runs.get(run_id)


# ===== 全局单例 =====

_facade: TeamRuntimeFacade | None = None


def get_team_runtime() -> TeamRuntimeFacade:
    """获取 TeamRuntimeFacade 全局单例。

    CLI / API / Web 路由应统一调用此函数而非直接构造 TeamRuntimeFacade，
    以保证全局活跃 Run 注册表 (_active_runs) 集中可查。
    """
    global _facade
    if _facade is None:
        _facade = TeamRuntimeFacade()
    return _facade
