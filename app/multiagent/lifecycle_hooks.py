"""Unified, audited lifecycle hook engine."""
from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id


class LifecycleEvent(str, Enum):
    RUN_CREATED = "RunCreated"
    RUN_STARTED = "RunStarted"
    TASK_CREATED = "TaskCreated"
    TASK_CLAIMED = "TaskClaimed"
    TASK_STARTED = "TaskStarted"
    BEFORE_TOOL_USE = "BeforeToolUse"
    AFTER_TOOL_USE = "AfterToolUse"
    PERMISSION_REQUESTED = "PermissionRequested"
    TASK_PRODUCED = "TaskProduced"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TEAMMATE_SPAWNED = "TeammateSpawned"
    TEAMMATE_IDLE = "TeammateIdle"
    TEAMMATE_STOPPED = "TeammateStopped"
    AGENT_MESSAGE = "AgentMessage"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_COMPLETED = "VerificationCompleted"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"


class HookResult(BaseModel):
    allow: bool = True
    block: bool = False
    feedback: str = ""
    mutate_metadata: dict[str, Any] = Field(default_factory=dict)
    request_human: bool = False
    request_replan: bool = False


@dataclass
class _Hook:
    hook_id: str
    event: LifecycleEvent
    handler: Callable[[dict[str, Any]], Any]
    scope: str
    priority: int
    timeout_seconds: float
    failure_policy: str


class LifecycleHookEngine:
    def __init__(self) -> None:
        self._hooks: list[_Hook] = []

    def register(
        self, event: LifecycleEvent, handler: Callable[[dict[str, Any]], Any],
        *, scope: str = "project", priority: int = 0,
        timeout_seconds: float = 5.0, failure_policy: str = "block",
    ) -> str:
        if scope not in {"system", "user", "project"}:
            raise ValueError("hook scope must be system, user or project")
        if failure_policy not in {"block", "allow"}:
            raise ValueError("hook failure_policy must be block or allow")
        hook = _Hook("hook_" + uuid.uuid4().hex[:12], event, handler, scope,
                     priority, timeout_seconds, failure_policy)
        self._hooks.append(hook)
        return hook.hook_id

    async def emit_async(
        self, event: LifecycleEvent, context: dict[str, Any],
    ) -> HookResult:
        combined = HookResult()
        order = {"system": 0, "user": 1, "project": 2}
        hooks = sorted((h for h in self._hooks if h.event == event),
                       key=lambda h: (order[h.scope], -h.priority, h.hook_id))
        for hook in hooks:
            try:
                if inspect.iscoroutinefunction(hook.handler):
                    raw = await asyncio.wait_for(hook.handler(dict(context)), hook.timeout_seconds)
                else:
                    raw = await asyncio.wait_for(asyncio.to_thread(hook.handler, dict(context)),
                                                 hook.timeout_seconds)
                result = raw if isinstance(raw, HookResult) else HookResult.model_validate(raw or {})
            except Exception as exc:
                result = HookResult(allow=hook.failure_policy == "allow",
                                    block=hook.failure_policy == "block",
                                    feedback=f"hook {hook.hook_id} failed: {exc}")
            combined.allow = combined.allow and result.allow and not result.block
            combined.block = combined.block or result.block
            combined.feedback = "\n".join(filter(None, [combined.feedback, result.feedback]))
            combined.mutate_metadata.update(result.mutate_metadata)
            combined.request_human = combined.request_human or result.request_human
            combined.request_replan = combined.request_replan or result.request_replan
            self._audit(event, hook, context, result)
            if combined.block:
                break
        return combined

    def emit(self, event: LifecycleEvent, context: dict[str, Any]) -> HookResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.emit_async(event, context))
        raise RuntimeError("emit() cannot run inside an event loop; use emit_async()")

    def register_command_hook(
        self, event: LifecycleEvent, argv: list[str], *, cwd: str,
        scope: str = "project", timeout_seconds: float = 10.0,
    ) -> str:
        def run_command(context: dict[str, Any]) -> HookResult:
            from app.multiagent.shell_policy import ShellCommandRunner
            result = ShellCommandRunner().run(argv, cwd=cwd, timeout=timeout_seconds)
            return HookResult(allow=result.returncode == 0, block=result.returncode != 0,
                              feedback=result.stderr or result.stdout)
        return self.register(event, run_command, scope=scope,
                             timeout_seconds=timeout_seconds + 1)

    @staticmethod
    def _audit(event: LifecycleEvent, hook: _Hook, context: dict[str, Any], result: HookResult) -> None:
        run_id = context.get("run_id")
        if not run_id:
            return
        get_agent_run_history().record_event(
            event_id=make_run_event_id(), run_id=run_id,
            event_type="LifecycleHook", agent_id=context.get("agent_id"),
            task_id=context.get("task_id"),
            payload={"hook_id": hook.hook_id, "lifecycle_event": event.value,
                     "scope": hook.scope, "result": result.model_dump(mode="json")},
        )


_engine: LifecycleHookEngine | None = None


def get_lifecycle_hook_engine() -> LifecycleHookEngine:
    global _engine
    if _engine is None:
        _engine = LifecycleHookEngine()
    return _engine


def reset_lifecycle_hook_engine() -> None:
    global _engine
    _engine = None
