"""Read-only operational diagnostics for one durable Run."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.infrastructure.database.run_store import get_agent_run_history
from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
from app.multiagent.task_board import BoardTaskStatus, get_task_board


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class RunDiagnosticsService:
    """Project TaskBoard, assignments, and events into a liveness snapshot."""

    def inspect(self, run_id: str) -> dict[str, Any] | None:
        history = get_agent_run_history()
        run = history.get_team_run(run_id)
        if run is None:
            return None

        board = get_task_board()
        board.restore_run(run_id)
        tasks = board.list_by_run(run_id)
        event_stats = history.event_envelope_stats(run_id)
        latest = event_stats.get("latest_event")
        latest_at = _parse_time(latest.get("timestamp") if latest else None)
        now = datetime.now(UTC)
        silence_seconds = (
            max(0.0, (now - latest_at).total_seconds())
            if latest_at
            else None
        )
        active = get_agent_runtime_manager().active_assignments(run_id)
        status = str(run.get("status", "created"))
        terminal = status in {"completed", "succeeded", "failed", "cancelled"}
        threshold = max(1, settings.stalled_run_threshold_seconds)

        task_counts = {state.value: 0 for state in BoardTaskStatus}
        for task in tasks:
            task_counts[task.status.value] += 1
        retryable = [
            task.task_id
            for task in tasks
            if task.status in {
                BoardTaskStatus.FAILED,
                BoardTaskStatus.REPAIR_REQUIRED,
                BoardTaskStatus.REPLAN_REQUIRED,
            }
        ]
        blockers = [
            {
                "task_id": task.task_id,
                "status": task.status.value,
                "message": task.last_error or "",
            }
            for task in tasks
            if task.status in {
                BoardTaskStatus.BLOCKED,
                BoardTaskStatus.FAILED,
                BoardTaskStatus.REPAIR_REQUIRED,
                BoardTaskStatus.REPLAN_REQUIRED,
            }
        ]
        delayed = [
            {
                "task_id": task.task_id,
                "next_attempt_at": task.next_attempt_at.isoformat(),
                "attempt": task.attempts,
                "max_attempts": task.max_attempts,
            }
            for task in tasks
            if task.status == BoardTaskStatus.PENDING
            and task.next_attempt_at is not None
        ]

        if status in {"failed", "cancelled"}:
            health = "failed"
        elif status in {"waiting_human", "paused"} or blockers:
            health = "attention"
        elif (
            not terminal
            and silence_seconds is not None
            and silence_seconds >= threshold
            and not active
        ):
            health = "stalled"
        elif terminal:
            health = "completed"
        else:
            health = "healthy"

        phase = ""
        if latest:
            event_type = str(latest.get("event_type", ""))
            phase = (
                event_type.removeprefix("root_graph:")
                .replace("_", " ")
                .strip()
            )
        recommended_action = {
            "failed": "检查错误详情后重试失败任务",
            "attention": "处理审批、权限或阻塞任务",
            "stalled": "运行已长时间无活动，可执行恢复",
            "completed": "运行已结束",
            "healthy": "运行正常，继续观察实时事件",
        }[health]
        return {
            "run_id": run_id,
            "status": status,
            "health": health,
            "phase": phase,
            "checked_at": now.isoformat(),
            "last_activity_at": latest_at.isoformat() if latest_at else None,
            "silence_seconds": round(silence_seconds, 1)
            if silence_seconds is not None
            else None,
            "stalled_threshold_seconds": threshold,
            "event_count": event_stats["event_count"],
            "last_sequence": event_stats["last_sequence"],
            "latest_event": latest,
            "active_assignments": [
                {
                    "agent_id": item.agent_id,
                    "task_id": item.task_id,
                    "session_id": item.session_id,
                }
                for item in active
            ],
            "task_counts": task_counts,
            "retryable_task_ids": retryable,
            "delayed_retries": delayed,
            "blockers": blockers,
            "recommended_action": recommended_action,
        }
