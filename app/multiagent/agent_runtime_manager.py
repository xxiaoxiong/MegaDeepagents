"""Runtime ownership for live TASK_TEAM teammate assignments.

This is deliberately part of the existing Scheduler → Executor path rather
than another multi-agent execution chain.  It records which stable
AgentInstance is currently executing which task and exposes a thread-safe,
cooperative cancellation signal to the Facade and executor tools.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActiveAssignment:
    lease_id: str
    run_id: str
    task_id: str
    agent_id: str
    session_id: str
    thread_id: str
    workspace_root: str


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
        self._active: dict[
            str, tuple[ActiveAssignment, CancellationToken, Any]
        ] = {}
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
        lease_id = "lease_" + uuid.uuid4().hex
        attempt_workspace = self._prepare_attempt_workspace(
            task_input,
            lease_id=lease_id,
            task_id=task_id,
        )
        assignment = ActiveAssignment(
            lease_id=lease_id,
            run_id=task_input["run_id"], task_id=task_id,
            agent_id=task_input["agent_id"], session_id=task_input["session_id"],
            thread_id=task_input["thread_id"],
            workspace_root=str(
                attempt_workspace or task_input.get("workspace_root") or ""
            ),
        )
        # The assignment token reaches tools and is controlled separately for
        # run cancellation and a single teammate stop.  Mutate the caller's
        # short-lived dict too: the Scheduler must inspect the exact token
        # after the worker returns before it can verify any result.
        token = CancellationToken(cancel_event)
        task_input["cancel_event"] = token
        task_input["assignment_lease_id"] = lease_id
        with self._lock:
            self._active[lease_id] = (assignment, token, agent_registry)

        def execute_in_lease() -> Any:
            try:
                return executor.execute_task(task_graph, task_id, task_input)
            finally:
                # Coroutine cancellation cannot stop a Python worker thread.
                # Ownership and cleanup therefore live in the worker wrapper,
                # which runs only after the underlying executor truly exits.
                with self._lock:
                    self._active.pop(lease_id, None)
                self._cleanup_attempt_workspace(attempt_workspace)

        return await asyncio.to_thread(execute_in_lease)

    @staticmethod
    def _safe_segment(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
        return normalized[:96] or "task"

    def _prepare_attempt_workspace(
        self,
        task_input: dict[str, Any],
        *,
        lease_id: str,
        task_id: str,
    ) -> Path | None:
        """Fence non-git workers inside one disposable attempt root.

        Git-backed assignments already execute in an isolated worktree and
        are fenced by the merge queue, so changing their root would break git
        semantics.  Normal workspace assignments use an attempt root while
        ArtifactStore remains rooted at the canonical Run workspace.
        """
        if task_input.get("worktree_mode"):
            return None
        raw_root = str(task_input.get("workspace_root") or "").strip()
        if not raw_root:
            return None
        canonical_root = Path(raw_root).resolve()
        attempts_root = (canonical_root / ".attempts").resolve()
        attempt_workspace = (
            attempts_root
            / self._safe_segment(task_id)
            / self._safe_segment(lease_id)
        ).resolve()
        if not attempt_workspace.is_relative_to(attempts_root):
            raise ValueError("attempt workspace escapes the run workspace")
        attempt_workspace.mkdir(parents=True, exist_ok=False)
        task_input["canonical_workspace_root"] = str(canonical_root)
        task_input["workspace_root"] = str(attempt_workspace)
        return attempt_workspace

    @staticmethod
    def _cleanup_attempt_workspace(attempt_workspace: Path | None) -> None:
        if attempt_workspace is None:
            return
        attempts_root = attempt_workspace.parent.parent.resolve()
        resolved = attempt_workspace.resolve()
        if (
            resolved == attempts_root
            or not resolved.is_relative_to(attempts_root)
            or attempts_root.name != ".attempts"
        ):
            return
        shutil.rmtree(resolved, ignore_errors=True)

    def active_assignments(self, run_id: str | None = None) -> list[ActiveAssignment]:
        with self._lock:
            return [
                assignment for assignment, _, _ in self._active.values()
                if run_id is None or assignment.run_id == run_id
            ]

    def cancel_run(self, run_id: str) -> int:
        """Signal all live assignments in a run without crossing event loops."""
        with self._lock:
            matching = [
                event
                for assignment, event, _ in self._active.values()
                if assignment.run_id == run_id
            ]
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
