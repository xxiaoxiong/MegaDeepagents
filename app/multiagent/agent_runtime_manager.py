"""Runtime ownership for live TASK_TEAM teammate assignments.

This is deliberately part of the existing Scheduler → Executor path rather
than another multi-agent execution chain.  It records which stable
AgentInstance is currently executing which task and exposes a thread-safe,
cooperative cancellation signal to the Facade and executor tools.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActiveAssignment:
    run_id: str
    task_id: str
    agent_id: str
    session_id: str
    thread_id: str


class CancellationToken:
    """Thread-safe cancellation view for one assignment.

    The token has a local signal for ``stop_agent`` and observes the run-wide
    signal supplied by the Facade/Scheduler.  It intentionally exposes the
    small ``Event`` surface used by executors, while keeping a teammate stop
    from mutating the whole run's event.
    """

    def __init__(self, run_event: Any) -> None:
        self._run_event = run_event
        self._local_event = threading.Event()

    def set(self) -> None:
        self._local_event.set()

    def is_set(self) -> bool:
        return self._local_event.is_set() or bool(self._run_event.is_set())

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for either signal without assuming the run event's loop."""
        if self.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self.is_set()
            if self._local_event.wait(0.05 if remaining is None else min(0.05, remaining)):
                return True
            if self._run_event.is_set():
                return True


class AgentRuntimeManager:
    """Own active teammate assignments for one process.

    DeepAgent session continuity is keyed by the AgentInstance's stable
    ``thread_id``.  The manager does not invent a second scheduler: it only
    mediates execution/cancellation for assignments already claimed by the
    authoritative TaskBoard.
    """

    def __init__(self) -> None:
        self._active: dict[tuple[str, str], tuple[ActiveAssignment, CancellationToken, Any]] = {}
        self._lock = threading.RLock()

    async def execute_assignment(
        self,
        *,
        executor: Any,
        task_graph: Any,
        task_id: str,
        task_input: dict[str, Any],
        cancel_event: Any,
        agent_registry: Any,
    ) -> Any:
        assignment = ActiveAssignment(
            run_id=task_input["run_id"], task_id=task_id,
            agent_id=task_input["agent_id"], session_id=task_input["session_id"],
            thread_id=task_input["thread_id"],
        )
        # The assignment token reaches tools and is controlled separately for
        # run cancellation and a single teammate stop.  Mutate the caller's
        # short-lived dict too: the Scheduler must inspect the exact token
        # after the worker returns before it can verify any result.
        token = CancellationToken(cancel_event)
        task_input["cancel_event"] = token
        key = (assignment.run_id, assignment.task_id)
        with self._lock:
            self._active[key] = (assignment, token, agent_registry)
        try:
            return await asyncio.to_thread(executor.execute_task, task_graph, task_id, task_input)
        finally:
            with self._lock:
                self._active.pop(key, None)

    def active_assignments(self, run_id: str | None = None) -> list[ActiveAssignment]:
        with self._lock:
            return [
                assignment for assignment, _, _ in self._active.values()
                if run_id is None or assignment.run_id == run_id
            ]

    def cancel_run(self, run_id: str) -> int:
        """Signal all live assignments in a run without crossing event loops."""
        with self._lock:
            matching = [event for assignment, event, _ in self._active.values() if assignment.run_id == run_id]
        for event in matching:
            event.set()
        return len(matching)

    def cancel_agent(self, run_id: str, agent_id: str) -> int:
        """Signal only assignments owned by one stable teammate."""
        with self._lock:
            matching = [
                event for assignment, event, _ in self._active.values()
                if assignment.run_id == run_id and assignment.agent_id == agent_id
            ]
        for event in matching:
            event.set()
        return len(matching)

    def _registry_for_agent(self, run_id: str, agent_id: str) -> Any:
        """Return the Registry that owns a live assignment, if any."""
        from app.multiagent.agent_registry import get_agent_registry

        with self._lock:
            for assignment, _, registry in self._active.values():
                if assignment.run_id == run_id and assignment.agent_id == agent_id:
                    return registry
        return get_agent_registry()

    def _owned_agent(self, run_id: str, agent_id: str) -> tuple[Any | None, Any]:
        registry = self._registry_for_agent(run_id, agent_id)
        agent = registry.get(agent_id)
        if agent is None or agent.run_id != run_id:
            return None, registry
        return agent, registry

    def pause_agent(self, run_id: str, agent_id: str) -> bool:
        """Block future claims for an idle teammate.

        Pausing an executing worker would require interrupting a tool call and
        can leave a task half-written.  Callers must use ``stop_agent`` for
        that cooperative cancellation path instead.
        """
        from app.multiagent.agent_instance import AgentStatus
        agent, registry = self._owned_agent(run_id, agent_id)
        if agent is None or agent.status != AgentStatus.IDLE:
            return False
        return registry.transition(agent_id, AgentStatus.BLOCKED)

    def resume_agent(self, run_id: str, agent_id: str) -> bool:
        """Return a paused teammate to the scheduler's eligible idle pool."""
        from app.multiagent.agent_instance import AgentStatus
        agent, registry = self._owned_agent(run_id, agent_id)
        if agent is None or agent.status != AgentStatus.BLOCKED:
            return False
        return registry.transition(agent_id, AgentStatus.IDLE)

    def stop_agent(self, run_id: str, agent_id: str) -> bool:
        """Cooperatively stop one teammate without cancelling its whole run."""
        agent, registry = self._owned_agent(run_id, agent_id)
        if agent is None:
            return False
        self.cancel_agent(run_id, agent_id)
        return registry.stop(agent_id, reason="runtime_stop")


_manager: AgentRuntimeManager | None = None


def get_agent_runtime_manager() -> AgentRuntimeManager:
    global _manager
    if _manager is None:
        _manager = AgentRuntimeManager()
    return _manager


def reset_agent_runtime_manager() -> None:
    global _manager
    _manager = None
