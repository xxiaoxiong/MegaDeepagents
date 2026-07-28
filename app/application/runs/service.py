"""Use-case layer for the unified run API."""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine

from app.core.logging import logger
from app.domain.runs.models import RunMode, RunStatus
from app.infrastructure.database.run_store import (
    get_agent_run_history,
    make_run_event_id,
)
from app.multiagent.team_run_context import TeamRunMode
from app.multiagent.team_runtime import get_team_runtime


class RunApplicationService:
    def __init__(self) -> None:
        self._background: set[asyncio.Task[Any]] = set()

    async def create(
        self,
        *,
        goal: str,
        mode: RunMode,
        team_template: str,
        repository_path: str | None,
        base_branch: str | None,
        review_required: bool,
        auto_approve_low_risk: bool,
        metadata: dict[str, Any],
        max_rounds: int = 80,
    ) -> dict[str, Any]:
        runtime = get_team_runtime()
        ctx = await runtime.create_run(
            goal=goal,
            team_name=team_template,
            mode=TeamRunMode.TASK_TEAM,
            max_rounds=max_rounds,
            review_required=review_required,
            source_repository_path=repository_path,
            base_branch=base_branch,
            requested_mode=mode.value,
            metadata={
                **metadata,
                "api_version": "v1",
                "auto_approve_low_risk": auto_approve_low_risk,
            },
        )
        # 记录首条用户消息事件，使其作为对话流的第一条用户气泡
        get_agent_run_history().record_event(
            event_id=make_run_event_id(),
            run_id=ctx.run_id,
            event_type="user_message",
            payload={"content": goal, "source": "human", "role": "user"},
        )
        self._spawn(
            runtime.start_run(ctx, goal, team_template, max_rounds, review_required),
            run_id=ctx.run_id,
        )
        return self.get(ctx.run_id) or {"run_id": ctx.run_id, "status": "running"}

    def get(self, run_id: str) -> dict[str, Any] | None:
        record = get_agent_run_history().get_team_run(run_id)
        return self._normalize(record) if record else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            self._normalize(record)
            for record in get_agent_run_history().list_team_runs(limit)
        ]

    async def pause(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run["status"] != RunStatus.RUNNING.value:
            return False
        return await get_team_runtime().pause_run(run_id)

    async def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run["status"] not in {
            RunStatus.CREATED.value,
            RunStatus.RUNNING.value,
            RunStatus.PAUSED.value,
            RunStatus.WAITING_HUMAN.value,
        }:
            return False
        return await get_team_runtime().cancel_run(run_id)

    async def resume(
        self,
        run_id: str,
        *,
        decision: str = "continue",
        feedback: str = "",
    ) -> bool:
        run = self.get(run_id)
        if run is None or run["status"] not in {
            RunStatus.PAUSED.value,
            RunStatus.WAITING_HUMAN.value,
        }:
            return False
        self._spawn(
            get_team_runtime().resume_run(
                run_id,
                resume_decision={
                    "decision": "deny" if decision == "deny" else "approve",
                    "feedback": feedback,
                },
            ),
            run_id=run_id,
        )
        return True

    async def retry(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        reason: str = "manual_retry",
        reset_attempts: bool = False,
    ) -> dict[str, Any] | None:
        """Requeue failed work and restart the same durable Run graph."""
        run = self.get(run_id)
        if run is None or run["status"] in {"succeeded", "cancelled"}:
            return None
        from app.multiagent.task_board import BoardTaskStatus, get_task_board

        board = get_task_board()
        board.restore_run(run_id)
        if task_id:
            candidates = [task_id]
        else:
            candidates = [
                task.task_id
                for task in board.list_by_run(run_id)
                if task.status in {
                    BoardTaskStatus.FAILED,
                    BoardTaskStatus.REPAIR_REQUIRED,
                    BoardTaskStatus.REPLAN_REQUIRED,
                }
            ]
        retried = [
            candidate
            for candidate in candidates
            if board.retry(
                candidate,
                run_id=run_id,
                reason=reason,
                reset_attempts=reset_attempts,
            )
        ]
        if not retried:
            return None
        history = get_agent_run_history()
        metadata = run.get("metadata") or {}
        generation = int(metadata.get("recovery_generation", 0)) + 1
        checkpoint_namespace = f"team:{run_id}:recovery:{generation}"
        history.merge_team_run_metadata(run_id, {
            "recovery_generation": generation,
            "checkpoint_namespace": checkpoint_namespace,
        })
        history.update_team_run_status(run_id, "running")
        history.record_event(
            event_id=make_run_event_id(),
            run_id=run_id,
            event_type="ManualRetryRequested",
            task_id=task_id,
            payload={
                "task_ids": retried,
                "reason": reason,
                "reset_attempts": reset_attempts,
                "recovery_generation": generation,
                "checkpoint_namespace": checkpoint_namespace,
            },
        )
        self._spawn(get_team_runtime().retry_run(run_id), run_id=run_id)
        return {
            "run_id": run_id,
            "status": "running",
            "retried_task_ids": retried,
            "recovery_generation": generation,
        }

    async def broadcast_message(self, run_id: str, content: str) -> int:
        """Deliver a user message through the same durable mailbox as agents."""
        history = get_agent_run_history()
        if self.get(run_id) is None:
            return 0
        # 记录用户消息事件，使其在对话流中显示为用户气泡
        history.record_event(
            event_id=make_run_event_id(),
            run_id=run_id,
            event_type="user_message",
            payload={"content": content, "source": "human", "role": "user"},
        )
        agent_ids = [
            item["agent_id"] for item in history.list_by_run(run_id)
            if item.get("status") not in {"stopped", "failed"}
        ]
        delivered = 0
        runtime = get_team_runtime()
        for agent_id in agent_ids:
            if await runtime.send_message(run_id, agent_id, content):
                delivered += 1
        return delivered

    async def stop_agent(self, run_id: str, agent_id: str) -> bool:
        return await get_team_runtime().stop_agent(run_id, agent_id)

    def recover_incomplete(self) -> int:
        # A waiting_human checkpoint must only continue after an explicit user
        # decision.  Automatically recovering it would silently approve the
        # suspended LangGraph interrupt.
        recoverable = {"created", "running"}
        count = 0
        for record in get_agent_run_history().list_team_runs(500):
            if record.get("status") in recoverable:
                self._spawn(
                    get_team_runtime().resume_run(record["run_id"]),
                    run_id=record["run_id"],
                )
                count += 1
        return count

    def _spawn(
        self, coroutine: Coroutine[Any, Any, Any], *, run_id: str
    ) -> None:
        async def guarded() -> Any:
            try:
                return await coroutine
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                history = get_agent_run_history()
                history.update_team_run_status(run_id, "failed")
                history.record_event(
                    event_id=make_run_event_id(),
                    run_id=run_id,
                    event_type="RunFailed",
                    payload={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "source": "background_task",
                    },
                )
                logger.exception("Background run failed run=%s", run_id)
                return None

        task = asyncio.create_task(guarded())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    def _normalize(record: dict[str, Any]) -> dict[str, Any]:
        status = record.get("status", "created")
        status = {
            "completed": "succeeded",
            "interrupted": "waiting_human",
            "incomplete": "failed",
        }.get(status, status)
        metadata = record.get("metadata") or {}
        return {
            "run_id": record["run_id"],
            "goal": record.get("goal", ""),
            "mode": metadata.get("requested_mode", "team"),
            "resolved_mode": metadata.get("resolved_mode"),
            "team_template": record.get("team_id", ""),
            "status": status,
            "workspace_root": record.get("workspace_root", ""),
            "review_required": bool(record.get("review_required", True)),
            "metadata": metadata,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }


_service: RunApplicationService | None = None


def get_run_service() -> RunApplicationService:
    global _service
    if _service is None:
        _service = RunApplicationService()
    return _service
