"""Read-only execution intelligence for a durable multi-agent Run.

The control plane already persists every fact needed to explain a Run, but
raw event streams force clients to reconstruct task ownership, concurrency,
critical-path pressure and agent activity themselves.  This projection keeps
those explanations deterministic and replayable without becoming a second
source of truth.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Iterable

from app.core.config import settings
from app.infrastructure.database.run_store import get_agent_run_history
from app.multiagent.task_board import get_task_board


_TERMINAL_RUN_STATUSES = {
    "completed",
    "succeeded",
    "failed",
    "cancelled",
}
_TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}
_ACTIVE_TASK_STATUSES = {"claimed", "running", "produced", "verifying"}
_BLOCKED_TASK_STATUSES = {
    "blocked",
    "failed",
    "repair_required",
    "replan_required",
}
_TASK_START_EVENTS = {"taskstarted"}
_TASK_END_EVENTS = {
    "taskproduced",
    "taskcompleted",
    "taskfailed",
    "taskfailedpermanently",
    "taskretryscheduled",
    "tasktimedout",
    "taskcancelled",
}
_NOISY_EVENT_TOKENS = {
    "assistanttoken",
    "taskheartbeat",
    "teammateheartbeat",
    "schedulerroundstarted",
}


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


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _normalized_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type", "")).removeprefix("root_graph:")


def _event_key(event: dict[str, Any]) -> str:
    return _normalized_type(event).replace("_", "").lower()


def _summary(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    for key in (
        "message",
        "error",
        "summary",
        "reason",
        "feedback",
        "tool",
        "tool_name",
        "verdict",
        "objective",
        "status",
    ):
        value = payload.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            return str(value)[:240]
        return str(value)[:240]
    return _normalized_type(event).replace("_", " ")


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id", ""),
        "run_id": event.get("run_id", ""),
        "agent_id": event.get("agent_id"),
        "task_id": event.get("task_id"),
        "event_type": event.get("event_type", ""),
        "sequence": int(event.get("sequence") or 0),
        "timestamp": event.get("timestamp", ""),
        "trace_id": event.get("trace_id"),
        "payload": event.get("payload") or {},
    }


def _longest_remaining_path(tasks: list[dict[str, Any]]) -> list[str]:
    """Return the longest unfinished dependency path in deterministic order."""
    by_id = {str(task["task_id"]): task for task in tasks}
    downstream: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        task_id = str(task["task_id"])
        for dependency in task.get("dependencies") or []:
            if dependency in by_id:
                downstream[str(dependency)].append(task_id)
    for values in downstream.values():
        values.sort()

    unfinished = {
        task_id
        for task_id, task in by_id.items()
        if str(task.get("status", "")) != "succeeded"
    }
    if not unfinished:
        return []

    memo: dict[str, list[str]] = {}

    def visit(task_id: str, trail: frozenset[str] = frozenset()) -> list[str]:
        if task_id in memo:
            return memo[task_id]
        if task_id in trail:
            return [task_id]
        candidates = [
            visit(child, trail | {task_id})
            for child in downstream.get(task_id, [])
            if child in unfinished
        ]
        best = max(candidates, key=lambda value: (len(value), value), default=[])
        memo[task_id] = [task_id, *best]
        return memo[task_id]

    roots = [
        task_id
        for task_id in unfinished
        if not any(
            dependency in unfinished
            for dependency in by_id[task_id].get("dependencies") or []
        )
    ]
    candidates = [visit(task_id) for task_id in sorted(roots or unfinished)]
    return max(candidates, key=lambda value: (len(value), value), default=[])


def _execution_intervals(
    events: Iterable[dict[str, Any]],
    fallback_end: datetime,
) -> list[tuple[datetime, datetime, str | None, str | None]]:
    starts: dict[tuple[str | None, str | None], datetime] = {}
    intervals: list[tuple[datetime, datetime, str | None, str | None]] = []
    for event in events:
        occurred_at = _parse_time(event.get("timestamp"))
        if occurred_at is None:
            continue
        key = (event.get("task_id"), event.get("agent_id"))
        kind = _event_key(event)
        if kind in _TASK_START_EVENTS:
            previous = starts.get(key)
            if previous is not None and occurred_at >= previous:
                intervals.append((previous, occurred_at, key[0], key[1]))
            starts[key] = occurred_at
        elif kind in _TASK_END_EVENTS and key in starts:
            started_at = starts.pop(key)
            if occurred_at >= started_at:
                intervals.append((started_at, occurred_at, key[0], key[1]))
    for (task_id, agent_id), started_at in starts.items():
        end = max(started_at, fallback_end)
        intervals.append((started_at, end, task_id, agent_id))
    return intervals


def _peak_concurrency(
    intervals: Iterable[tuple[datetime, datetime, str | None, str | None]],
) -> int:
    points: list[tuple[datetime, int]] = []
    for started_at, ended_at, _, _ in intervals:
        points.append((started_at, 1))
        points.append((ended_at, -1))
    # End events sort first at the same instant, preventing adjacent work from
    # being reported as overlapping.
    points.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    for _, delta in points:
        active += delta
        peak = max(peak, active)
    return peak


def _tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical = [event for event in events if _event_key(event) == "aftertooluse"]
    if canonical:
        return canonical
    return [event for event in events if _event_key(event) == "toolcallresult"]


class RunExecutionIntelligenceService:
    """Project persisted Run facts into an operator-focused explanation."""

    def inspect(self, run_id: str) -> dict[str, Any] | None:
        history = get_agent_run_history()
        run = history.get_team_run(run_id)
        if run is None:
            return None

        board = get_task_board()
        if not board.list_by_run(run_id):
            board.restore_run(run_id)
        tasks = [
            task.model_dump(mode="json")
            for task in board.list_by_run(run_id)
        ]
        agents = history.list_by_run(run_id)
        artifacts = history.list_artifacts_by_run(run_id)
        event_stats = history.event_envelope_stats(run_id)
        last_sequence = int(event_stats["last_sequence"])
        events = history.list_event_envelopes(
            run_id,
            after_sequence=max(0, last_sequence - 2_000),
            limit=2_000,
        )
        now = datetime.now(UTC)
        created_at = _parse_time(run.get("created_at")) or now
        latest_at = _parse_time(
            (event_stats.get("latest_event") or {}).get("timestamp")
        )
        terminal = str(run.get("status", "")) in _TERMINAL_RUN_STATUSES
        fallback_end = (
            _parse_time(run.get("updated_at")) or latest_at or now
            if terminal
            else now
        )
        fallback_end = max(created_at, fallback_end)
        wall_time_ms = max(
            0,
            round((fallback_end - created_at).total_seconds() * 1_000),
        )

        intervals = _execution_intervals(events, fallback_end)
        active_time_ms = round(
            sum((end - start).total_seconds() * 1_000 for start, end, _, _ in intervals)
        )
        tool_events = _tool_events(events)
        retries = [
            event
            for event in events
            if any(
                token in _event_key(event)
                for token in ("retry", "repair", "replan")
            )
        ]
        handoffs = [
            event
            for event in events
            if any(
                token in _event_key(event)
                for token in ("agentmessage", "handoff", "delegat")
            )
        ]
        critical_path = _longest_remaining_path(tasks)
        critical_ids = set(critical_path)
        task_by_id = {str(task["task_id"]): task for task in tasks}
        events_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        events_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("agent_id"):
                events_by_agent[str(event["agent_id"])].append(event)
            if event.get("task_id"):
                events_by_task[str(event["task_id"])].append(event)

        artifact_ids_by_agent: dict[str, list[str]] = defaultdict(list)
        artifact_ids_by_task: dict[str, list[str]] = defaultdict(list)
        for artifact in artifacts:
            artifact_id = str(artifact.get("artifact_id", ""))
            if artifact.get("produced_by"):
                artifact_ids_by_agent[str(artifact["produced_by"])].append(artifact_id)
            if artifact.get("task_id"):
                artifact_ids_by_task[str(artifact["task_id"])].append(artifact_id)

        task_projection = []
        for task in tasks:
            task_id = str(task["task_id"])
            dependencies = [str(item) for item in task.get("dependencies") or []]
            blocked_by = [
                dependency
                for dependency in dependencies
                if str(task_by_id.get(dependency, {}).get("status", "missing"))
                != "succeeded"
            ]
            task_events = events_by_task.get(task_id, [])
            latest_task_event = task_events[-1] if task_events else None
            task_projection.append(
                {
                    "task_id": task_id,
                    "title": task.get("title") or task.get("objective") or task_id,
                    "status": str(task.get("status", "")),
                    "claimed_by": task.get("claimed_by"),
                    "dependencies": dependencies,
                    "blocked_by": blocked_by,
                    "critical": task_id in critical_ids,
                    "attempts": int(task.get("attempts") or 0),
                    "max_attempts": int(task.get("max_attempts") or 0),
                    "artifact_ids": artifact_ids_by_task.get(task_id, []),
                    "last_activity_at": (
                        latest_task_event.get("timestamp")
                        if latest_task_event
                        else task.get("updated_at")
                    ),
                }
            )

        agent_projection = []
        for agent in agents:
            agent_id = str(agent.get("agent_id", ""))
            agent_events = events_by_agent.get(agent_id, [])
            agent_tools = _tool_events(agent_events)
            owned_tasks = [
                task
                for task in task_projection
                if task["claimed_by"] == agent_id
            ]
            completed_task_ids = sorted(
                {
                    str(event["task_id"])
                    for event in agent_events
                    if event.get("task_id")
                    and _event_key(event) == "taskcompleted"
                }
                | {
                    task["task_id"]
                    for task in owned_tasks
                    if task["status"] == "succeeded"
                }
            )
            assigned_task_ids = sorted(
                {
                    str(event["task_id"])
                    for event in agent_events
                    if event.get("task_id")
                    and _event_key(event)
                    in {"taskclaimed", "taskstarted", "taskcompleted"}
                }
                | {task["task_id"] for task in owned_tasks}
            )
            recent_events = [
                _public_event(event)
                for event in reversed(agent_events)
                if _event_key(event) not in _NOISY_EVENT_TOKENS
            ][:12]
            produced = sorted(
                set(artifact_ids_by_agent.get(agent_id, []))
                | set(artifact_ids_by_agent.get(str(agent.get("name", "")), []))
            )
            current_task_id = agent.get("current_task_id")
            current_task = task_by_id.get(str(current_task_id), {})
            agent_projection.append(
                {
                    "agent_id": agent_id,
                    "name": agent.get("name") or agent.get("role") or agent_id,
                    "role": agent.get("role") or "",
                    "status": str(agent.get("status", "")),
                    "current_task_id": current_task_id,
                    "current_task_title": (
                        current_task.get("title")
                        or current_task.get("objective")
                        or None
                    ),
                    "capabilities": agent.get("capabilities") or [],
                    "assigned_task_ids": assigned_task_ids,
                    "completed_task_ids": completed_task_ids,
                    "artifact_ids": produced,
                    "event_count": len(agent_events),
                    "tool_call_count": len(agent_tools),
                    "last_activity_at": (
                        agent_events[-1].get("timestamp")
                        if agent_events
                        else agent.get("updated_at")
                    ),
                    "latest_summary": _summary(agent_events[-1])
                    if agent_events
                    else "尚未记录执行活动",
                    "recent_events": recent_events,
                }
            )
        agent_projection.sort(
            key=lambda item: (
                item["status"] not in {"running", "claiming"},
                item["name"],
            )
        )

        attention: list[dict[str, Any]] = []
        for task in task_projection:
            if task["status"] in _BLOCKED_TASK_STATUSES:
                source = task_by_id[task["task_id"]]
                attention.append(
                    {
                        "severity": "error"
                        if task["status"] == "failed"
                        else "warning",
                        "kind": "task_blocker",
                        "title": f"{task['title']} 需要处理",
                        "detail": source.get("last_error")
                        or f"任务状态为 {task['status']}",
                        "task_id": task["task_id"],
                        "agent_id": task["claimed_by"],
                    }
                )
            elif task["attempts"] > 1:
                attention.append(
                    {
                        "severity": "warning",
                        "kind": "retry_pressure",
                        "title": f"{task['title']} 已重试 {task['attempts'] - 1} 次",
                        "detail": "建议检查重复失败原因，避免继续消耗模型与工具预算。",
                        "task_id": task["task_id"],
                        "agent_id": task["claimed_by"],
                    }
                )
        if terminal and tasks and not artifacts:
            attention.append(
                {
                    "severity": "warning",
                    "kind": "missing_delivery",
                    "title": "运行结束但没有可交付 Artifact",
                    "detail": "结果无法形成可点击、可追溯的正式交付物。",
                    "task_id": None,
                    "agent_id": None,
                }
            )
        stale_threshold = max(1, settings.stalled_run_threshold_seconds)
        for agent in agent_projection:
            if agent["status"] not in {"running", "claiming"}:
                continue
            last_at = _parse_time(agent["last_activity_at"])
            if last_at and (now - last_at).total_seconds() >= stale_threshold:
                attention.append(
                    {
                        "severity": "warning",
                        "kind": "stale_agent",
                        "title": f"{agent['name']} 长时间没有活动",
                        "detail": f"超过 {stale_threshold} 秒未产生可观测事件。",
                        "task_id": agent["current_task_id"],
                        "agent_id": agent["agent_id"],
                    }
                )

        active_agents = max(
            1,
            len(
                {
                    agent_id
                    for _, _, _, agent_id in intervals
                    if agent_id is not None
                }
            ),
        )
        parallelism = (
            round(active_time_ms / wall_time_ms, 2) if wall_time_ms else 0.0
        )
        utilization = (
            round(min(1.0, active_time_ms / (wall_time_ms * active_agents)), 3)
            if wall_time_ms
            else 0.0
        )
        return {
            "run_id": run_id,
            "generated_at": now.isoformat(),
            "summary": {
                "event_count": int(event_stats["event_count"]),
                "wall_time_ms": wall_time_ms,
                "active_time_ms": active_time_ms,
                "parallelism": parallelism,
                "utilization": utilization,
                "peak_concurrency": _peak_concurrency(intervals),
                "tool_call_count": len(tool_events),
                "retry_count": len(retries),
                "handoff_count": len(handoffs),
                "artifact_count": len(artifacts),
                "completed_tasks": sum(
                    task["status"] == "succeeded" for task in task_projection
                ),
                "total_tasks": len(task_projection),
                "critical_path": critical_path,
                "critical_path_remaining": len(critical_path),
            },
            "agents": agent_projection,
            "tasks": task_projection,
            "attention": attention,
        }
