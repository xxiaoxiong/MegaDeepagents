"""TaskBoard — 共享子任务板。

Phase D：所有 Agent 通过原子认领抢占任务。
不依赖 TaskScheduler 的内部 [], 而用持久化认领状态。

认领契约（docs/MegaDeepagents_Agent_Teams_改造任务书.md §9）：
- claim(task_id, agent_id) → 成功 / 已被认领
- release(task_id, agent_id) → 释放回 pending
- complete(task_id, agent_id, artifacts) → 标记 succeeded
- fail(task_id, agent_id, error) → 标记 failed
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import logger


class BoardTaskStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    PRODUCED = "produced"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    REPAIR_REQUIRED = "repair_required"
    REPLAN_REQUIRED = "replan_required"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class BoardTask(BaseModel):
    """Board 上的一个共享任务（原子认领单元）。"""
    task_id: str
    run_id: str
    title: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)

    status: BoardTaskStatus = BoardTaskStatus.PENDING
    claimed_by: str | None = None
    claimed_at: datetime | None = None

    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None

    produced_artifact_ids: list[str] = Field(default_factory=list)
    priority: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClaimResult(BaseModel):
    success: bool
    task: BoardTask | None = None
    reason: str = ""


class TaskBoard:
    """共享任务板（进程内 + 锁安全）。"""

    def __init__(self) -> None:
        import threading
        # A local task id is only unique inside one TeamRun.  Keeping a
        # composite key here prevents two concurrent runs from overwriting
        # each other (both planners commonly emit ``task_1``).
        self._tasks: dict[tuple[str, str], BoardTask] = {}
        self._by_run: dict[str, list[tuple[str, str]]] = {}
        self._lock = threading.RLock()

    # ===== 添加 =====

    def add(self, task: BoardTask) -> BoardTask:
        with self._lock:
            key = (task.run_id, task.task_id)
            self._tasks[key] = task
            keys = self._by_run.setdefault(task.run_id, [])
            if key not in keys:
                keys.append(key)
            logger.debug(f"[TaskBoard] added task={task.task_id} run={task.run_id}")
            return task

    def create_task(
        self,
        task_id: str,
        run_id: str,
        title: str,
        objective: str,
        dependencies: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> BoardTask:
        task = BoardTask(
            task_id=task_id,
            run_id=run_id,
            title=title,
            objective=objective,
            dependencies=dependencies or [],
            required_capabilities=required_capabilities or [],
            priority=priority,
            max_attempts=max_attempts,
        )
        return self.add(task)

    # ===== 原子认领 =====

    def claim(self, task_id: str, agent_id: str, run_id: str | None = None) -> ClaimResult:
        """原子认领。如果 task 已被认领或不在 PENDING 状态，返回失败。"""
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None:
                return ClaimResult(success=False, reason="task_not_found")
            if task.status != BoardTaskStatus.PENDING:
                return ClaimResult(
                    success=False,
                    task=task,
                    reason=f"task_not_pending({task.status.value})",
                )
            # 检查依赖是否已完成
            for dep_id in task.dependencies:
                dep = self._tasks.get((task.run_id, dep_id))
                if dep is None or dep.status != BoardTaskStatus.SUCCEEDED:
                    return ClaimResult(
                        success=False,
                        task=task,
                        reason=f"dependency_{dep_id}_not_succeeded",
                    )
            task.status = BoardTaskStatus.CLAIMED
            task.claimed_by = agent_id
            task.claimed_at = datetime.utcnow()
            task.updated_at = datetime.utcnow()
            logger.info(
                f"[TaskBoard] claimed task={task_id} agent={agent_id}"
            )
            return ClaimResult(success=True, task=task)

    def start(self, task_id: str, agent_id: str, run_id: str | None = None) -> bool:
        """CLAIMED → RUNNING。"""
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status != BoardTaskStatus.CLAIMED:
                return False
            task.status = BoardTaskStatus.RUNNING
            task.updated_at = datetime.utcnow()
            return True

    def release(self, task_id: str, agent_id: str, reason: str = "", run_id: str | None = None) -> bool:
        """释放回 PENDING（让其他 Agent 认领）。"""
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status not in (BoardTaskStatus.CLAIMED, BoardTaskStatus.RUNNING, BoardTaskStatus.BLOCKED):
                return False
            task.status = BoardTaskStatus.PENDING
            task.claimed_by = None
            task.claimed_at = None
            task.attempts += 1
            task.last_error = reason or None
            task.updated_at = datetime.utcnow()
            logger.info(f"[TaskBoard] released task={task_id} agent={agent_id} reason={reason}")
            return True

    def complete(
        self,
        task_id: str, agent_id: str, artifact_ids: list[str] | None = None,
        run_id: str | None = None,
    ) -> bool:
        """标记 succeeded。"""
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            task.status = BoardTaskStatus.SUCCEEDED
            task.produced_artifact_ids.extend(artifact_ids or [])
            task.completed_at = datetime.utcnow()
            task.updated_at = datetime.utcnow()
            logger.info(
                f"[TaskBoard] completed task={task_id} agent={agent_id} "
                f"artifacts={len(artifact_ids or [])}"
            )
            return True

    def mark_produced(
        self, task_id: str, agent_id: str, artifact_ids: list[str] | None,
        run_id: str | None = None,
    ) -> bool:
        """Record worker output; only the verifier may mark SUCCEEDED."""
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status != BoardTaskStatus.RUNNING:
                return False
            task.status = BoardTaskStatus.PRODUCED
            task.produced_artifact_ids = list(dict.fromkeys(artifact_ids or []))
            task.updated_at = datetime.utcnow()
            return True

    def mark_verifying(self, task_id: str, run_id: str | None = None) -> bool:
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.status != BoardTaskStatus.PRODUCED:
                return False
            task.status = BoardTaskStatus.VERIFYING
            task.updated_at = datetime.utcnow()
            return True

    def mark_verified(self, task_id: str, run_id: str | None = None) -> bool:
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.status not in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                return False
            task.status = BoardTaskStatus.SUCCEEDED
            task.completed_at = datetime.utcnow()
            task.updated_at = datetime.utcnow()
            return True

    def mark_repair_required(self, task_id: str, run_id: str | None = None) -> bool:
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.status not in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                return False
            task.status = BoardTaskStatus.REPAIR_REQUIRED
            task.updated_at = datetime.utcnow()
            return True

    def fail(self, task_id: str, agent_id: str, error: str, run_id: str | None = None) -> bool:
        """标记 failed（或重置为 PENDING 如果还有重试次数）。"""
        with self._lock:
            task = self.get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            task.attempts += 1
            task.last_error = error
            task.updated_at = datetime.utcnow()
            if task.attempts < task.max_attempts:
                # 重置为 pending
                task.status = BoardTaskStatus.PENDING
                task.claimed_by = None
                task.claimed_at = None
                logger.warning(
                    f"[TaskBoard] task={task_id} failed (attempt {task.attempts}/{task.max_attempts}), "
                    f"reset to pending"
                )
            else:
                task.status = BoardTaskStatus.FAILED
                task.completed_at = datetime.utcnow()
                logger.warning(
                    f"[TaskBoard] task={task_id} failed permanently: {error}"
                )
            return True

    # ===== 查询 =====

    def get(self, task_id: str, run_id: str | None = None) -> BoardTask | None:
        """Return a task, requiring a run id whenever it is ambiguous.

        The optional argument preserves the old single-run API without
        silently selecting a task from another concurrent run.
        """
        if run_id is not None:
            return self._tasks.get((run_id, task_id))
        matches = [task for (rid, tid), task in self._tasks.items() if tid == task_id]
        return matches[0] if len(matches) == 1 else None

    def list_by_run(self, run_id: str) -> list[BoardTask]:
        keys = self._by_run.get(run_id, [])
        return [self._tasks[key] for key in keys if key in self._tasks]

    def list_pending(self, run_id: str) -> list[BoardTask]:
        return [
            t for t in self.list_by_run(run_id)
            if t.status == BoardTaskStatus.PENDING
        ]

    def list_claimable(
        self, run_id: str, agent_id: str, capabilities: list[str] | None = None,
    ) -> list[BoardTask]:
        """返回该 Agent 当前可认领的任务列表（依赖已满足 + capability 匹配）。"""
        result = []
        for t in self.list_pending(run_id):
            # 依赖检查
            if not all(
                self._tasks.get((run_id, dep)) is not None and
                self._tasks[(run_id, dep)].status == BoardTaskStatus.SUCCEEDED
                for dep in t.dependencies
            ):
                continue
            # 能力检查
            if capabilities and t.required_capabilities:
                if not set(t.required_capabilities).issubset(set(capabilities)):
                    continue
            result.append(t)
        result.sort(key=lambda x: -x.priority)
        return result

    def all_succeeded(self, run_id: str) -> bool:
        tasks = self.list_by_run(run_id)
        if not tasks:
            return True
        return all(t.status == BoardTaskStatus.SUCCEEDED for t in tasks)

    def all_produced(self, run_id: str) -> bool:
        tasks = self.list_by_run(run_id)
        return bool(tasks) and all(
            task.status in (BoardTaskStatus.PRODUCED, BoardTaskStatus.SUCCEEDED)
            for task in tasks
        )

    def summary(self, run_id: str) -> dict[str, int]:
        tasks = self.list_by_run(run_id)
        summary = {s.value: 0 for s in BoardTaskStatus}
        for t in tasks:
            summary[t.status.value] += 1
        summary["total"] = len(tasks)
        return summary


# ===== 全局单例 =====

_board: TaskBoard | None = None


def get_task_board() -> TaskBoard:
    global _board
    if _board is None:
        _board = TaskBoard()
    return _board


def reset_task_board() -> None:
    global _board
    _board = None
