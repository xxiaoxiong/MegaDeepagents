"""TeamRuntimeFacade — 统一团队运行时入口。

docs/MegaDeepagents_Agent_Teams_改造任务书.md §7.2：
CLI、API、Web 只能调用该 Facade，不得直接实例化旧 TeamRunner 或 SimpleOrchestrator。
"""
from __future__ import annotations

import asyncio
import os
import threading
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
        source_repository_path: str | None = None,
        base_branch: str | None = None,
        base_commit_sha: str | None = None,
        environment_file_allowlist: list[str] | None = None,
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
        if source_repository_path:
            from app.multiagent.git_workspace import RepositoryWorkspaceManager
            repository = RepositoryWorkspaceManager(
                source_repository_path, ctx.workspace_root,
                base_branch=base_branch, base_commit_sha=base_commit_sha,
            )
            ctx.metadata["repository"] = {
                "source_repository_path": repository.source_repository,
                "base_branch": repository.base_branch,
                "base_commit_sha": repository.base_commit_sha,
                "environment_file_allowlist": list(environment_file_allowlist or ()),
            }
        else:
            ctx.metadata["workspace_provider"] = "local"
        self._active_runs[ctx.run_id] = {
            "ctx": ctx,
            "goal": goal,
            "team_name": team_name,
            "mode": mode,
            "max_rounds": max_rounds,
            "review_required": review_required,
            "status": "created",
            # Scheduler runs in a worker thread. threading.Event is safe for
            # API cancellation from the serving loop as well as that worker.
            "cancel_event": threading.Event(),
            "created_at": datetime.utcnow(),
        }
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().save_team_run(
            run_id=ctx.run_id, goal=goal, team_id=team_name, mode=mode.value,
            workspace_root=ctx.workspace_root, status="created", max_rounds=max_rounds,
            review_required=review_required,
            metadata=ctx.metadata,
        )
        from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
        hook = await get_lifecycle_hook_engine().emit_async(
            LifecycleEvent.RUN_CREATED,
            {"run_id": ctx.run_id, "goal": goal, "team_id": team_name,
             "mode": mode.value, "metadata": ctx.metadata},
        )
        if hook.block or not hook.allow:
            get_agent_run_history().update_team_run_status(ctx.run_id, "failed")
            self._active_runs[ctx.run_id]["status"] = "failed"
            raise PermissionError(hook.feedback or "RunCreated hook blocked run")
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
        if ctx.run_id not in self._active_runs:
            self._activate_restored_run(ctx, goal, team_name, max_rounds, review_required)
        self._active_runs[ctx.run_id]["status"] = "running"
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().update_team_run_status(ctx.run_id, "running")
        from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
        hook = await get_lifecycle_hook_engine().emit_async(
            LifecycleEvent.RUN_STARTED,
            {"run_id": ctx.run_id, "goal": goal, "team_id": team_name},
        )
        if hook.block or not hook.allow:
            self._active_runs[ctx.run_id]["status"] = "failed"
            get_agent_run_history().update_team_run_status(ctx.run_id, "failed")
            return TeamRunResult(
                task_id=ctx.run_id, status="failed", final_output="",
                termination_reason=hook.feedback or "RunStarted hook blocked run",
                completed_at=datetime.utcnow(),
            )

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
        *,
        resume: bool = False,
    ) -> TeamRunResult:
        """TASK_TEAM 模式：使用 Phase Two 基于 TaskGraph 的编排器。"""
        from app.multiagent.executor import DeepAgentExecutor
        from app.multiagent.verifier import Verifier, LLMRubricVerifier
        from app.multiagent.planner import plan_with_llm
        from app.multiagent.orchestrator import run_orchestrated
        from app.multiagent.artifact import ArtifactStore
        from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine

        # 为本次 Run 创建 ArtifactStore（root_path = run workspace）
        artifact_store = ArtifactStore(root_path=ctx.workspace_root)
        if resume:
            artifact_store.load_from_db(ctx.run_id)

        # 创建 Executor 并注入 workspace 和 artifact_store
        executor = DeepAgentExecutor(workspace_root=ctx.workspace_root)
        executor.set_artifact_store(artifact_store)

        # Production verification is fail-closed.  Model/configuration errors
        # can never turn a non-empty artifact into PASS.
        verifier = Verifier(
            llm_rubric=LLMRubricVerifier(model_available=True, fail_closed=True),
            artifact_store=artifact_store,
        )

        # 串联运行
        resume_graph = self._task_graph_from_persisted_board(ctx.run_id) if resume else None
        # run_orchestrated contains synchronous planning/executor adapters.
        # Offloading keeps the API event loop responsive for message/cancel.
        result = await asyncio.to_thread(
            run_orchestrated,
            goal=goal,
            mode_override="full_multi",
            planner=lambda g, c: plan_with_llm(g, context=c),
            executor=executor,
            verifier=verifier,
            ctx=ctx,
            cancel_event=self._active_runs[ctx.run_id]["cancel_event"],
            task_graph=resume_graph,
        )

        # 映射结果
        status_map = {
            "completed": "completed",
            "failed": "failed",
            "interrupted": (
                "paused" if result.error == "paused"
                else "waiting_human" if result.error and "waiting" in result.error
                else "cancelled"
            ),
            "incomplete": "failed",
        }
        self._active_runs[ctx.run_id]["status"] = status_map.get(result.status, "failed")
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().update_team_run_status(ctx.run_id, self._active_runs[ctx.run_id]["status"])
        final_status = self._active_runs[ctx.run_id]["status"]
        final_event = (LifecycleEvent.RUN_COMPLETED if final_status == "completed"
                       else LifecycleEvent.RUN_FAILED)
        await get_lifecycle_hook_engine().emit_async(
            final_event,
            {"run_id": ctx.run_id, "status": final_status,
             "error": result.error, "verdict": result.verification_verdict},
        )

        return TeamRunResult(
            task_id=ctx.run_id,
            status=final_status,
            final_output=result.summary or goal[:200],
            total_rounds=result.rounds,
            termination_reason=result.error,
            completed_at=datetime.utcnow(),
        )

    def _task_graph_from_persisted_board(self, run_id: str):
        """Load the full persisted plan and overlay current TaskBoard state.

        Older runs created before graph snapshots still fall back to rebuilding
        the board projection.  New runs never discard output contracts/budgets
        merely because the process restarted.
        """
        from app.multiagent.task_board import get_task_board, BoardTaskStatus
        from app.multiagent.task_graph import TaskGraph, TaskNode, TaskNodeStatus
        from app.multiagent.phase_g_store import get_agent_run_history

        board = get_task_board()
        board.restore_run(run_id)
        tasks = board.list_by_run(run_id)
        snapshot = get_agent_run_history().load_task_graph(run_id)
        if snapshot:
            try:
                graph = TaskGraph.model_validate(snapshot)
            except Exception as exc:
                logger.warning("[TeamRuntime] invalid TaskGraph snapshot run=%s: %s", run_id, exc)
                graph = None
        else:
            graph = None
        if graph is None and not tasks:
            return None
        if graph is None:
            graph = TaskGraph(root_task_id=tasks[0].task_id)
        status_map = {
            BoardTaskStatus.SUCCEEDED: TaskNodeStatus.SUCCEEDED,
            BoardTaskStatus.FAILED: TaskNodeStatus.FAILED,
            BoardTaskStatus.CANCELLED: TaskNodeStatus.CANCELLED,
        }
        for task in tasks:
            if task.task_id not in graph.nodes:
                graph.add_node(TaskNode(
                    id=task.task_id, title=task.title, objective=task.objective,
                    dependencies=task.dependencies,
                    required_capabilities=task.required_capabilities,
                    priority=task.priority, max_attempts=task.max_attempts,
                ))
            node = graph.nodes[task.task_id]
            node.status = status_map.get(task.status, TaskNodeStatus.PENDING)
            node.output_artifact_ids = list(task.produced_artifact_ids)
        return graph

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
            history = get_agent_run_history()
            if not history.get_team_run(run_id):
                return False
            # A cold cancellation must mutate the persisted board too.  On a
            # later resume, pending work may not silently revive.
            from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
            from app.multiagent.task_board import get_task_board
            get_agent_runtime_manager().cancel_run(run_id)
            board = get_task_board()
            board.restore_run(run_id)
            board.cancel_run(run_id)
            return history.update_team_run_status(run_id, "cancelled")
        run["cancel_event"].set()
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        get_agent_runtime_manager().cancel_run(run_id)
        from app.multiagent.task_board import get_task_board
        get_task_board().cancel_run(run_id)
        run["status"] = "cancelled"
        from app.multiagent.phase_g_store import get_agent_run_history
        get_agent_run_history().update_team_run_status(run_id, "cancelled")
        return True

    async def pause_run(self, run_id: str) -> bool:
        """Cooperatively pause scheduling without cancelling durable tasks."""
        run = await self.get_run(run_id)
        if not run:
            return False
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        from app.multiagent.agent_registry import get_agent_registry
        for agent in get_agent_registry().list_by_run(run_id):
            get_agent_runtime_manager().pause_agent(run_id, agent.agent_id)
        if run_id in self._active_runs:
            self._active_runs[run_id]["status"] = "paused"
        from app.multiagent.phase_g_store import get_agent_run_history
        return get_agent_run_history().update_team_run_status(run_id, "paused")

    async def pause_agent(self, run_id: str, agent_id: str) -> bool:
        """Pause one idle teammate in the same registry the Scheduler uses."""
        if not await self.get_run(run_id):
            return False
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        return get_agent_runtime_manager().pause_agent(run_id, agent_id)

    async def resume_agent(self, run_id: str, agent_id: str) -> bool:
        """Make one paused teammate eligible for atomic reservation again."""
        if not await self.get_run(run_id):
            return False
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        return get_agent_runtime_manager().resume_agent(run_id, agent_id)

    async def stop_agent(self, run_id: str, agent_id: str) -> bool:
        """Cooperatively stop one teammate owned by this TeamRuntime run."""
        if not await self.get_run(run_id):
            return False
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        return get_agent_runtime_manager().stop_agent(run_id, agent_id)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._active_runs.get(run_id)
        if run is not None:
            return run
        from app.multiagent.phase_g_store import get_agent_run_history
        return get_agent_run_history().get_team_run(run_id)

    async def send_message(self, run_id: str, agent_id: str, message: str) -> bool:
        """向运行中的 Agent 发送消息（通过 Mailbox 治理钩子）。"""
        from app.multiagent.mailbox import MailboxMessage, get_mailbox, make_message_id

        run = await self.get_run(run_id)
        if not run:
            return False
        from app.multiagent.agent_registry import get_agent_registry
        target = get_agent_registry().get(agent_id)
        if target is None or target.run_id != run_id:
            # Cold runs may not yet be rehydrated; durable identity is still
            # authoritative for message routing.
            from app.multiagent.phase_g_store import get_agent_run_history
            stored = get_agent_run_history().get_agent_instance(agent_id)
            if not stored or stored.get("run_id") != run_id:
                return False
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
            if ok:
                from app.multiagent.teammate_session import (
                    TeammateCommandQueue, TeammateCommandType, get_teammate_supervisor,
                )
                session = get_teammate_supervisor().load(agent_id)
                if session is not None:
                    TeammateCommandQueue(session.session_id).put(
                        TeammateCommandType.MESSAGE.value, msg.model_dump(mode="json")
                    )
            logger.info(
                f"[TeamRuntime] send_message run={run_id} agent={agent_id} "
                f"ok={ok} msg={message[:80]}"
            )
            return ok
        except Exception as exc:
            logger.error(f"[TeamRuntime] send_message failed: {exc}")
            return False

    async def resume_run(self, run_id: str) -> bool:
        """Restore durable state and continue the same TASK_TEAM execution."""
        from app.multiagent.resume_coordinator import get_resume_coordinator
        from app.multiagent.phase_g_store import get_agent_run_history

        run = self._active_runs.get(run_id)
        if not run:
            stored = get_agent_run_history().get_team_run(run_id)
            if not stored:
                return False
            logger.info("[TeamRuntime] cold resume run=%s from durable state", run_id)
            try:
                mode = TeamRunMode.from_legacy(stored.get("mode", "task_team"))
                ctx = TeamRunContext(
                    run_id=run_id,
                    team_id=stored["team_id"],
                    mode=mode,
                    workspace_root=stored["workspace_root"],
                    checkpoint_namespace=f"team:{run_id}",
                    user_goal=stored.get("goal", ""),
                    metadata=stored.get("metadata") or {},
                )
                os.makedirs(ctx.workspace_root, exist_ok=True)
                run = self._activate_restored_run(
                    ctx, stored.get("goal", ""), stored["team_id"],
                    int(stored.get("max_rounds", 20)), bool(stored.get("review_required", True)),
                )
            except Exception as exc:
                logger.error("[TeamRuntime] failed to reconstruct run=%s: %s", run_id, exc)
                return False
        try:
            coordinator = get_resume_coordinator()
            result = coordinator.resume(run_id)
            run["status"] = "running"
            get_agent_run_history().update_team_run_status(run_id, "running")
            logger.info(f"[TeamRuntime] resume_run result run={run_id}: {result.to_dict()}")
            ctx = run["ctx"]
            if ctx.mode == TeamRunMode.TASK_TEAM:
                task_result = await self._run_task_team(
                    ctx, run["goal"], run["team_name"], run["max_rounds"],
                    run["review_required"], resume=True,
                )
            else:
                task_result = await self._run_discussion(
                    ctx, run["goal"], run["team_name"], run["max_rounds"], run["review_required"],
                )
            run["status"] = task_result.status
            get_agent_run_history().update_team_run_status(run_id, task_result.status)
            return task_result.status not in ("failed", "cancelled")
        except Exception as exc:
            logger.error(f"[TeamRuntime] resume_run failed run={run_id}: {exc}")
            return False

    def _activate_restored_run(
        self, ctx: TeamRunContext, goal: str, team_name: str,
        max_rounds: int, review_required: bool,
    ) -> dict[str, Any]:
        info = {
            "ctx": ctx, "goal": goal, "team_name": team_name,
            "mode": ctx.mode, "max_rounds": max_rounds,
            "review_required": review_required, "status": "created",
            "cancel_event": threading.Event(), "created_at": ctx.created_at,
        }
        self._active_runs[ctx.run_id] = info
        return info

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

    def list_run_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return active and cold-restart TASK_TEAM runs without a second store."""
        from app.multiagent.phase_g_store import get_agent_run_history
        records = {record["run_id"]: record for record in get_agent_run_history().list_team_runs(limit)}
        for run_id, active in self._active_runs.items():
            records[run_id] = {
                "run_id": run_id, "goal": active.get("goal", ""),
                "team_id": active.get("team_name", ""),
                "mode": getattr(active.get("mode"), "value", active.get("mode")),
                "status": active.get("status", "unknown"),
                "max_rounds": active.get("max_rounds"),
                "review_required": active.get("review_required"),
                "created_at": active.get("created_at", ""),
            }
        return list(records.values())[:limit]


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
