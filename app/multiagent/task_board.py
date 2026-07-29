"""TaskBoard — 共享子任务板。

所有 Agent 通过原子认领抢占任务。
不依赖调度器的进程内列表，而用持久化认领状态。

原子认领契约：
- claim(task_id, agent_id) → 成功 / 已被认领
- release(task_id, agent_id) → 释放回 pending
- complete(task_id, agent_id, artifacts) → 标记 succeeded
- fail(task_id, agent_id, error) → 标记 failed
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import logger


# Extra retries granted to rate-limited (429) failures beyond the normal
# max_attempts budget.  RetryPolicy.decide() returns retryable=False once
# attempts >= max_attempts, so without this grace a single 429 at the tail
# of the retry budget permanently fails the task.  429s are transient
# (gateway throttle windows are typically 10-60s); 5 grace retries with
# 15s exponential backoff (15/30/60/120/240s, capped at 300s) gives the
# gateway ample time to recover.  See run_2a438328372441d8 for the failure
# mode this prevents.
_RATE_LIMIT_GRACE = 5


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
    next_attempt_at: datetime | None = None

    produced_artifact_ids: list[str] = Field(default_factory=list)
    priority: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClaimResult(BaseModel):
    success: bool
    task: BoardTask | None = None
    reason: str = ""


class TaskBoard:
    """共享任务板（进程内缓存 + 可选 SQLite durable source）。"""

    def __init__(self, persist: bool = False) -> None:
        import threading
        # A local task id is only unique inside one TeamRun.  Keeping a
        # composite key here prevents two concurrent runs from overwriting
        # each other (both planners commonly emit ``task_1``).
        self._tasks: dict[tuple[str, str], BoardTask] = {}
        self._by_run: dict[str, list[tuple[str, str]]] = {}
        self._lock = threading.RLock()
        self._persist_enabled = persist

    def _persist(self, task: BoardTask) -> None:
        """Best-effort durable write; scheduling must not silently lose state."""
        if not self._persist_enabled:
            return
        try:
            from app.infrastructure.database.run_store import get_agent_run_history
            get_agent_run_history().upsert_task_board_task(task.model_dump(mode="json"))
        except Exception as exc:
            # A failed durable write makes recovery unsafe.  Surface it to the
            # caller through logs instead of pretending the board is durable.
            logger.error("[TaskBoard] persist failed run=%s task=%s: %s", task.run_id, task.task_id, exc)
            raise

    # ===== 添加 =====

    def add(self, task: BoardTask) -> BoardTask:
        with self._lock:
            key = (task.run_id, task.task_id)
            self._tasks[key] = task
            keys = self._by_run.setdefault(task.run_id, [])
            if key not in keys:
                keys.append(key)
            self._persist(task)
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
            task = self._raw_get(task_id, run_id=run_id)
            if task is None:
                return ClaimResult(success=False, reason="task_not_found")
            if self._persist_enabled:
                # BEGIN IMMEDIATE serializes claimers across threads and
                # processes.  The database row, not a stale in-memory copy,
                # decides the winner.
                from app.multiagent.store import _get_conn
                conn = _get_conn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT payload FROM task_board_tasks WHERE run_id=? AND task_id=?",
                        (task.run_id, task_id),
                    ).fetchone()
                    if row is None:
                        conn.rollback()
                        return ClaimResult(success=False, reason="task_not_found")
                    task = BoardTask.model_validate(json.loads(row["payload"]))
                    if task.status != BoardTaskStatus.PENDING:
                        conn.rollback()
                        return ClaimResult(
                            success=False, task=task,
                            reason=f"task_not_pending({task.status.value})",
                        )
                    if task.dependencies:
                        # N+1 dependency check collapsed into a single round
                        # trip.  The previous per-dep SELECT was a 1+N query
                        # pattern on high-fan-in DAGs (10 deps = 11 reads).
                        placeholders = ",".join("?" for _ in task.dependencies)
                        dep_rows = conn.execute(
                            f"SELECT task_id, payload FROM task_board_tasks "
                            f"WHERE run_id=? AND task_id IN ({placeholders})",
                            (task.run_id, *task.dependencies),
                        ).fetchall()
                        dep_map: dict[str, BoardTask | None] = {}
                        for row in dep_rows:
                            try:
                                dep_map[row["task_id"]] = BoardTask.model_validate(
                                    json.loads(row["payload"])
                                )
                            except Exception:
                                dep_map[row["task_id"]] = None
                        for dep_id in task.dependencies:
                            dep = dep_map.get(dep_id)
                            if dep is None or dep.status != BoardTaskStatus.SUCCEEDED:
                                conn.rollback()
                                return ClaimResult(
                                    success=False, task=task,
                                    reason=f"dependency_{dep_id}_not_succeeded",
                                )
                    task.status = BoardTaskStatus.CLAIMED
                    task.claimed_by = agent_id
                    task.claimed_at = datetime.now(UTC)
                    task.updated_at = datetime.now(UTC)
                    conn.execute(
                        "UPDATE task_board_tasks SET payload=?, updated_at=? "
                        "WHERE run_id=? AND task_id=?",
                        (json.dumps(task.model_dump(mode="json")),
                         task.updated_at.isoformat(), task.run_id, task.task_id),
                    )
                    conn.commit()
                    self._tasks[(task.run_id, task.task_id)] = task
                    logger.info("[TaskBoard] claimed task=%s agent=%s", task_id, agent_id)
                    return ClaimResult(success=True, task=task)
                except Exception:
                    conn.rollback()
                    raise
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
            task.claimed_at = datetime.now(UTC)
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            logger.info(
                f"[TaskBoard] claimed task={task_id} agent={agent_id}"
            )
            return ClaimResult(success=True, task=task)

    def start(self, task_id: str, agent_id: str, run_id: str | None = None) -> bool:
        """CLAIMED → RUNNING。"""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status != BoardTaskStatus.CLAIMED:
                return False
            task.status = BoardTaskStatus.RUNNING
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def release(self, task_id: str, agent_id: str, reason: str = "", run_id: str | None = None) -> bool:
        """释放回 PENDING（让其他 Agent 认领）。"""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status not in (BoardTaskStatus.CLAIMED, BoardTaskStatus.RUNNING, BoardTaskStatus.BLOCKED):
                return False
            task.status = BoardTaskStatus.PENDING
            task.claimed_by = None
            task.claimed_at = None
            task.attempts += 1
            task.last_error = reason or None
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            logger.info(f"[TaskBoard] released task={task_id} agent={agent_id} reason={reason}")
            return True

    def complete(
        self,
        task_id: str, agent_id: str, artifact_ids: list[str] | None = None,
        run_id: str | None = None,
    ) -> bool:
        """标记 succeeded。"""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            task.status = BoardTaskStatus.SUCCEEDED
            task.produced_artifact_ids.extend(artifact_ids or [])
            task.completed_at = datetime.now(UTC)
            task.updated_at = datetime.now(UTC)
            self._persist(task)
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
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status != BoardTaskStatus.RUNNING:
                return False
            task.status = BoardTaskStatus.PRODUCED
            task.produced_artifact_ids = list(dict.fromkeys(artifact_ids or []))
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def mark_verifying(self, task_id: str, run_id: str | None = None) -> bool:
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status != BoardTaskStatus.PRODUCED:
                return False
            task.status = BoardTaskStatus.VERIFYING
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def mark_verified(self, task_id: str, run_id: str | None = None) -> bool:
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status not in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                return False
            task.status = BoardTaskStatus.SUCCEEDED
            task.completed_at = datetime.now(UTC)
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def mark_repair_required(self, task_id: str, run_id: str | None = None) -> bool:
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status not in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                return False
            task.status = BoardTaskStatus.REPAIR_REQUIRED
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def mark_blocked(
        self, task_id: str, agent_id: str, reason: str, run_id: str | None = None,
    ) -> bool:
        """Atomically transition a worker's RUNNING task to BLOCKED.

        Replaces the get→mutate→add pattern that previously exposed the shared
        stored object to concurrent mutation.  Only the claiming agent may
        block its own task, mirroring ``release``/``complete``.
        """
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            if task.status != BoardTaskStatus.RUNNING:
                return False
            task.status = BoardTaskStatus.BLOCKED
            task.last_error = reason
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def supersede_with_repair(self, task_id: str, repair_task_id: str, run_id: str) -> bool:
        """Record verifier-owned replacement without forging worker success."""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status != BoardTaskStatus.REPAIR_REQUIRED:
                return False
            task.metadata["superseded_by_repair"] = repair_task_id
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def fail(
        self,
        task_id: str,
        agent_id: str,
        error: str,
        run_id: str | None = None,
        *,
        retryable: bool = True,
        retry_delay_seconds: float = 0.0,
        failure_category: str = "unknown",
    ) -> bool:
        """标记 failed（或重置为 PENDING 如果还有重试次数）。"""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.claimed_by != agent_id:
                return False
            task.attempts += 1
            task.last_error = error
            task.updated_at = datetime.now(UTC)
            history = task.metadata.setdefault("error_history", [])
            history.append({
                "attempt": task.attempts,
                "category": failure_category,
                "message": error,
                "timestamp": task.updated_at.isoformat(),
            })
            if len(history) > 20:
                del history[:-20]
            if retryable and task.attempts < task.max_attempts:
                # 重置为 pending
                task.status = BoardTaskStatus.PENDING
                task.claimed_by = None
                task.claimed_at = None
                task.next_attempt_at = (
                    task.updated_at + timedelta(seconds=max(0.0, retry_delay_seconds))
                )
                logger.warning(
                    f"[TaskBoard] task={task_id} failed (attempt {task.attempts}/{task.max_attempts}), "
                    f"retry after {max(0.0, retry_delay_seconds):.1f}s"
                )
            elif (
                failure_category == "rate_limited"
                and task.metadata.get("rate_limit_grace_used", 0) < _RATE_LIMIT_GRACE
            ):
                # 429 宽限重试 —— RetryPolicy.decide() 在 max_attempts 耗尽时
                # 返回 retryable=False / delay_seconds=0.0，所以正常分支不会触发。
                # 但 429 是临时错误：网关限流窗口（10-60s）过去后通常会恢复。
                # 这里给 5 次额外宽限重试，用 15s 指数退避（15/30/60/120/240s，
                # 封顶 300s）让网关有时间恢复。
                #
                # 没有这个分支，run_2a438328372441d8 T01 在 max_attempts=2 耗尽后
                # 直接永久失败，15s 退避根本没机会执行。
                grace_used = task.metadata.get("rate_limit_grace_used", 0)
                task.metadata["rate_limit_grace_used"] = grace_used + 1
                task.status = BoardTaskStatus.PENDING
                task.claimed_by = None
                task.claimed_at = None
                exponent = max(0, task.attempts - 1)
                grace_delay = min(300.0, 15.0 * (2 ** exponent))
                task.next_attempt_at = (
                    task.updated_at + timedelta(seconds=grace_delay)
                )
                logger.warning(
                    f"[TaskBoard] task={task_id} rate-limited "
                    f"(grace {grace_used + 1}/{_RATE_LIMIT_GRACE}, "
                    f"attempt {task.attempts}/{task.max_attempts}), "
                    f"retry after {grace_delay:.1f}s"
                )
            else:
                task.status = BoardTaskStatus.FAILED
                task.completed_at = datetime.now(UTC)
                task.next_attempt_at = None
                logger.warning(
                    f"[TaskBoard] task={task_id} failed permanently: {error}"
                )
            self._persist(task)
            return True

    def retry(
        self,
        task_id: str,
        *,
        run_id: str,
        reason: str = "manual_retry",
        reset_attempts: bool = False,
    ) -> bool:
        """Requeue a terminal task through an explicit, audited operator action."""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status not in {
                BoardTaskStatus.FAILED,
                BoardTaskStatus.REPAIR_REQUIRED,
                BoardTaskStatus.REPLAN_REQUIRED,
                BoardTaskStatus.BLOCKED,
            }:
                return False
            task.metadata.setdefault("manual_retries", []).append({
                "reason": reason,
                "previous_status": task.status.value,
                "previous_attempts": task.attempts,
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if reset_attempts:
                task.attempts = 0
            elif task.attempts >= task.max_attempts:
                task.max_attempts = task.attempts + 1
            task.status = BoardTaskStatus.PENDING
            task.claimed_by = None
            task.claimed_at = None
            task.completed_at = None
            task.next_attempt_at = None
            task.last_error = reason
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def cancel(self, task_id: str, reason: str = "cancelled", run_id: str | None = None) -> bool:
        """Terminal cancellation owned by the runtime, never by the worker."""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status in (
                BoardTaskStatus.SUCCEEDED, BoardTaskStatus.FAILED, BoardTaskStatus.CANCELLED,
            ):
                return False
            task.status = BoardTaskStatus.CANCELLED
            task.last_error = reason
            task.completed_at = datetime.now(UTC)
            task.updated_at = datetime.now(UTC)
            self._persist(task)
            return True

    def request_replan(
        self,
        task_id: str,
        reason: str,
        *,
        requested_by: str,
        run_id: str | None = None,
    ) -> bool:
        """Persist a control-plane replan fence against stale worker success."""
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            if task is None or task.status == BoardTaskStatus.CANCELLED:
                return False
            requested_at = datetime.now(UTC)
            requests = task.metadata.setdefault("replan_requests", [])
            requests.append({
                "requested_by": requested_by,
                "reason": reason,
                "requested_at": requested_at.isoformat(),
                "previous_status": task.status.value,
            })
            if len(requests) > 20:
                del requests[:-20]
            task.status = BoardTaskStatus.REPLAN_REQUIRED
            task.last_error = reason
            task.completed_at = None
            task.updated_at = requested_at
            self._persist(task)
            return True

    def cancel_run(self, run_id: str, reason: str = "run_cancelled") -> int:
        """Cancel every non-terminal task so a persisted run cannot revive it."""
        with self._lock:
            task_ids = [task.task_id for task in self._raw_list_by_run(run_id)]
        return sum(1 for task_id in task_ids if self.cancel(task_id, reason, run_id))

    def purge_run(self, run_id: str) -> int:
        """Evict a finished run's tasks from the in-process cache.

        The process-level ``_tasks``/``_by_run`` dicts previously lived for the
        whole process lifetime: nothing ever removed finished runs, so a long
        uptime service accumulated every historical task and ``list_by_run`` /
        ``list_pending`` scans grew linearly.  The durable rows in SQLite are
        untouched and ``restore_run`` can rehydrate the board if the run is
        ever resumed.  Returns the number of tasks evicted.
        """
        evicted = 0
        with self._lock:
            keys = self._by_run.pop(run_id, [])
            for key in keys:
                if self._tasks.pop(key, None) is not None:
                    evicted += 1
        return evicted

    # ===== restart recovery =====

    def restore_run(self, run_id: str) -> int:
        """Hydrate a run's board from SQLite without inventing any task state."""
        if not self._persist_enabled:
            return 0
        try:
            from app.infrastructure.database.run_store import get_agent_run_history
            payloads = get_agent_run_history().list_task_board_tasks(run_id)
        except Exception as exc:
            logger.error("[TaskBoard] restore failed run=%s: %s", run_id, exc)
            raise
        restored = 0
        with self._lock:
            for payload in payloads:
                task = BoardTask.model_validate(payload)
                key = (task.run_id, task.task_id)
                if key in self._tasks:
                    continue
                self._tasks[key] = task
                self._by_run.setdefault(task.run_id, []).append(key)
                restored += 1
        return restored

    def prepare_for_resume(self, run_id: str) -> int:
        """Release leases held by a dead process while preserving completed work.

        CLAIMED/RUNNING tasks clearly belonged to a dead worker and go back to
        PENDING.  PRODUCED/VERIFYING tasks are the zombie window: the worker
        finished and produced artifacts, but the process died before the
        verifier could run.  Leaving them in place means ``list_pending`` never
        returns them and ``all_succeeded`` never sees SUCCEEDED, so the run
        deadlocks on ``scheduler_deadlock`` even though the work is recoverable.
        Requeue them to PENDING while preserving ``produced_artifact_ids`` so a
        retry can reuse the output.
        """
        changed = 0
        with self._lock:
            for task in self._raw_list_by_run(run_id):
                if task.status not in (
                    BoardTaskStatus.CLAIMED, BoardTaskStatus.RUNNING,
                    BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING,
                ):
                    continue
                resume_reason = {
                    BoardTaskStatus.PRODUCED: "interrupted_after_produced",
                    BoardTaskStatus.VERIFYING: "interrupted_during_verify",
                }.get(task.status, "interrupted_before_resume")
                task.status = BoardTaskStatus.PENDING
                task.claimed_by = None
                task.claimed_at = None
                task.last_error = resume_reason
                task.updated_at = datetime.now(UTC)
                self._persist(task)
                changed += 1
        return changed

    # ===== 查询 =====

    def _raw_get(self, task_id: str, run_id: str | None = None) -> BoardTask | None:
        """Internal accessor returning the live stored object reference.

        Board mutation methods run under ``self._lock`` and must mutate the
        stored object in place so the change is visible to the next locker.
        External callers must use ``get`` which returns a defensive copy.
        """
        if run_id is not None:
            return self._tasks.get((run_id, task_id))
        matches = [task for (rid, tid), task in self._tasks.items() if tid == task_id]
        return matches[0] if len(matches) == 1 else None

    def _raw_list_by_run(self, run_id: str) -> list[BoardTask]:
        """Internal accessor returning live stored object references."""
        keys = self._by_run.get(run_id, [])
        return [self._tasks[key] for key in keys if key in self._tasks]

    def get(self, task_id: str, run_id: str | None = None) -> BoardTask | None:
        """Return a defensive copy of a task, requiring a run id when ambiguous.

        Callers receive a deep copy so a read-modify-write sequence outside the
        board cannot mutate the shared stored object while another thread is
        iterating it (which previously raised ``dictionary changed size during
        iteration``).  State changes must go back through ``add`` or an atomic
        board method.
        """
        with self._lock:
            task = self._raw_get(task_id, run_id=run_id)
            return task.model_copy(deep=True) if task is not None else None

    def list_by_run(self, run_id: str) -> list[BoardTask]:
        """Return defensive copies of every task in a run."""
        with self._lock:
            return [task.model_copy(deep=True) for task in self._raw_list_by_run(run_id)]

    def list_pending(self, run_id: str) -> list[BoardTask]:
        now = datetime.now(UTC)
        return [
            t for t in self.list_by_run(run_id)
            if t.status == BoardTaskStatus.PENDING
            and (t.next_attempt_at is None or t.next_attempt_at <= now)
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
        return all(
            t.status == BoardTaskStatus.SUCCEEDED
            or bool(t.metadata.get("superseded_by_plan_revision"))
            or (
                t.status == BoardTaskStatus.REPAIR_REQUIRED
                and bool(t.metadata.get("superseded_by_repair"))
            )
            for t in tasks
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
        _board = TaskBoard(persist=True)
    return _board


def reset_task_board() -> None:
    global _board
    _board = None
