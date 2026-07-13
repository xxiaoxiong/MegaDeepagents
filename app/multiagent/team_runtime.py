"""TeamRuntimeFacade — 统一团队运行时入口。

docs/MegaDeepagents_Agent_Teams_改造任务书.md §7.2：
CLI、API、Web 只能调用该 Facade，不得直接实例化旧 TeamRunner 或 SimpleOrchestrator。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.team_run_context import TeamRunContext, TeamRunMode
from app.multiagent.agent_spec import TeamRunResult


class TeamRuntimeFacade:
    """统一团队运行时门面。

    所有团队任务入口（CLI、API、Web）必须通过此门面。
    内部根据 mode 自动路由到 TASK_TEAM 或 DISCUSSION 运行时。
    """

    def __init__(self) -> None:
        self._active_runs: dict[str, dict[str, Any]] = {}

    # ===== Run 生命周期 =====

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
        """创建一次新的团队运行，返回上下文。"""
        ctx = TeamRunContext.create(
            goal=goal,
            team_name=team_name,
            mode=mode,
            workspace_root=workspace_root,
            user_id=user_id,
        )
        logger.info(
            f"[TeamRuntime] run created: id={ctx.run_id} team={team_name} "
            f"mode={mode.value} workspace={ctx.workspace_root}"
        )
        self._active_runs[ctx.run_id] = {
            "ctx": ctx,
            "goal": goal,
            "team_name": team_name,
            "mode": mode,
            "max_rounds": max_rounds,
            "review_required": review_required,
            "status": "created",
            "cancel_event": asyncio.Event(),
            "created_at": datetime.utcnow(),
        }
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().save_team_run(
            run_id=ctx.run_id, goal=goal, team_id=team_name, mode=mode.value,
            workspace_root=ctx.workspace_root, status="created", max_rounds=max_rounds,
            review_required=review_required,
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
        """启动团队运行。"""
        self._active_runs[ctx.run_id]["status"] = "running"
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().update_team_run_status(ctx.run_id, "running")

        if ctx.mode == TeamRunMode.DISCUSSION:
            return await self._run_discussion(
                ctx, goal, team_name, max_rounds, review_required,
            )
        return await self._run_task_team(
            ctx, goal, team_name, max_rounds, review_required,
        )

    # ===== 运行时 =====

    async def _run_task_team(
        self,
        ctx: TeamRunContext,
        goal: str,
        team_name: str,
        max_rounds: int,
        review_required: bool,
    ) -> TeamRunResult:
        """TASK_TEAM 模式：使用 Phase Two 基于 TaskGraph 的编排器。"""
        from app.multiagent.executor import DeepAgentExecutor
        from app.multiagent.verifier import Verifier, LLMRubricVerifier
        from app.multiagent.planner import plan_with_llm
        from app.multiagent.orchestrator import run_orchestrated
        from app.multiagent.artifact import ArtifactStore

        # 为本次 Run 创建 ArtifactStore（root_path = run workspace）
        artifact_store = ArtifactStore(root_path=ctx.workspace_root)

        # 创建 Executor 并注入 workspace 和 artifact_store
        executor = DeepAgentExecutor(workspace_root=ctx.workspace_root)
        executor.set_artifact_store(artifact_store)

        # 构造 Verifier（带程序化 + LLM 回退）
        verifier = Verifier(
            llm_rubric=LLMRubricVerifier(model_available=False),
            artifact_store=artifact_store,
        )

        # 串联运行
        result = run_orchestrated(
            goal=goal,
            mode_override="full_multi",
            planner=lambda g, c: plan_with_llm(g, context=c),
            executor=executor,
            verifier=verifier,
            ctx=ctx,
            cancel_event=self._active_runs[ctx.run_id]["cancel_event"],
        )

        # 映射结果
        status_map = {
            "completed": "completed",
            "failed": "failed",
            "interrupted": "cancelled",
            "incomplete": "failed",
        }
        self._active_runs[ctx.run_id]["status"] = status_map.get(result.status, "failed")
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().update_team_run_status(ctx.run_id, self._active_runs[ctx.run_id]["status"])

        return TeamRunResult(
            task_id=ctx.run_id,
            status=status_map.get(result.status, "failed"),
            final_output=result.summary or goal[:200],
            total_rounds=result.rounds,
            termination_reason=result.error,
            completed_at=datetime.utcnow(),
        )

    async def _run_discussion(
        self,
        ctx: TeamRunContext,
        goal: str,
        team_name: str,
        max_rounds: int,
        review_required: bool,
    ) -> TeamRunResult:
        """DISCUSSION 模式：使用旧 TeamRunner。"""
        from app.multiagent.team_runner import TeamRunner, _run_team_traced

        runner = TeamRunner.create(
            goal=goal,
            team_name=team_name,
            max_rounds=max_rounds,
            review_required=review_required,
            task_id=ctx.run_id,
        )

        result = _run_team_traced(runner)
        self._active_runs[ctx.run_id]["status"] = result.status
        return result

    # ===== 辅助操作 =====

    async def cancel_run(self, run_id: str) -> bool:
        run = self._active_runs.get(run_id)
        if not run:
            from app.multiagent.phase_g_store import get_agent_run_history
            return get_agent_run_history().update_team_run_status(run_id, "cancelled")
        run["cancel_event"].set()
        run["status"] = "cancelled"
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().update_team_run_status(run_id, "cancelled")
        return True

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._active_runs.get(run_id)
        if run is not None:
            return run
        from app.multiagent.phase_g_store import get_agent_run_history
        return get_agent_run_history().get_team_run(run_id)

    async def send_message(self, run_id: str, agent_id: str, message: str) -> bool:
        """向运行中的 Agent 发送消息（通过 Mailbox 治理钩子）。"""
        from app.multiagent.mailbox import MailboxMessage, get_mailbox, make_message_id

        mailbox = get_mailbox()
        try:
            msg = MailboxMessage(
                message_id=make_message_id(),
                from_agent_id="user",  # 外部用户消息
                from_agent_name="user",
                to_agent_id=agent_id,
                run_id=run_id,
                title="user_inject",
                content=message,
            )
            ok = mailbox.send(msg)
            logger.info(
                f"[TeamRuntime] send_message run={run_id} agent={agent_id} "
                f"ok={ok} msg={message[:80]}"
            )
            return ok
        except Exception as exc:
            logger.error(f"[TeamRuntime] send_message failed: {exc}")
            return False

    async def resume_run(self, run_id: str) -> bool:
        """恢复一次运行：调用 ResumeCoordinator 加载持久化的 Agent 与已完成的 Task。"""
        from app.multiagent.resume_coordinator import get_resume_coordinator

        run = self._active_runs.get(run_id)
        if not run:
            # 也可以从 history 表里反查（Phase G 第 1 步：跨进程重启）
            logger.info(f"[TeamRuntime] resume_run: run={run_id} 不在内存 _active_runs，尝试从持久化恢复")
        try:
            coordinator = get_resume_coordinator()
            result = coordinator.resume(run_id)
            if run is not None:
                run["status"] = "running"
            logger.info(f"[TeamRuntime] resume_run result run={run_id}: {result.to_dict()}")
            return True
        except Exception as exc:
            logger.error(f"[TeamRuntime] resume_run failed run={run_id}: {exc}")
            return False

    def list_runs(self) -> list[dict[str, Any]]:
        return [
            {
                "run_id": rid,
                "team_name": info.get("team_name"),
                "mode": info.get("mode", {}).value if hasattr(info.get("mode"), "value") else str(info.get("mode", "")),
                "status": info["status"],
                "created_at": info["created_at"].isoformat() if isinstance(info["created_at"], datetime) else str(info["created_at"]),
            }
            for rid, info in self._active_runs.items()
        ]


# ===== 全局单例 =====

_facade: TeamRuntimeFacade | None = None


def get_team_runtime() -> TeamRuntimeFacade:
    global _facade
    if _facade is None:
        _facade = TeamRuntimeFacade()
    return _facade


def reset_team_runtime() -> None:
    global _facade
    _facade = None
