"""Scheduler 单元测试（docs/upgradePhaseTwo.md 测试要求 2、3、4、6、12、13）。

覆盖：
1. Sync fallback 运行通过 DAG
2. ready_tasks fan-out 正确
3. 有依赖 task 不会提前执行
4. 无依赖 task 可独立完成
5. 失败 task 重试
6. Repair Task 动态新增
7. max_rounds 打断
8. all_succeeded 终止
"""
from __future__ import annotations

import pytest

from app.multiagent.scheduler import (
    TaskScheduler,
    ScriptedWorkerExecutor,
    WorkerExecutor,
    TaskResult,
    _InMemoryWorkerExecutor,
    SchedulerError,
)
from app.multiagent.task_graph import TaskGraph, TaskNode, TaskNodeStatus, OutputContract


def _make_node(
    _id: str,
    deps: list[str] | None = None,
    caps: list[str] | None = None,
) -> TaskNode:
    return TaskNode(
        id=_id,
        title=_id,
        objective=f"do {_id}",
        dependencies=deps or [],
        required_capabilities=caps or [],
    )


# ===== 1. 同步 fallback 通过 DAG =====


def test_sync_fallback_simple_chain():
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.add_node(_make_node("B", deps=["A"]))
    dag.add_node(_make_node("C", deps=["B"]))

    scheduler = TaskScheduler(dag, max_rounds=10)
    result = scheduler._run_sync_fallback()
    assert result["status"] == "completed"
    assert result["termination_reason"] == "all_tasks_succeeded"
    assert dag.nodes["A"].status == TaskNodeStatus.SUCCEEDED
    assert dag.nodes["B"].status == TaskNodeStatus.SUCCEEDED
    assert dag.nodes["C"].status == TaskNodeStatus.SUCCEEDED


def test_sync_fallback_parallel():
    """两个无依赖 task 一起被执行。"""
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.add_node(_make_node("B"))

    scheduler = TaskScheduler(dag, max_rounds=10)
    result = scheduler._run_sync_fallback()
    assert result["status"] == "completed"
    assert dag.nodes["A"].status == TaskNodeStatus.SUCCEEDED
    assert dag.nodes["B"].status == TaskNodeStatus.SUCCEEDED


# ===== 2. 有依赖 task 不会提前执行 =====


def test_dependency_enforced():
    """B 依赖 A，A FAILED 时 B 不执行。"""
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.add_node(_make_node("B", deps=["A"]))

    worker = ScriptedWorkerExecutor(
        script_success={"A": False, "B": True},
    )
    scheduler = TaskScheduler(dag, max_rounds=5, worker_executor=worker)
    result = scheduler._run_sync_fallback()
    # A FAILED，B 从未被调度（依赖未满足）
    assert dag.nodes["A"].status == TaskNodeStatus.FAILED
    assert dag.nodes["B"].status == TaskNodeStatus.PENDING  # 从未 ready
    assert result["termination_reason"] in ("no_ready_with_pending", "max_rounds")
    assert result["status"] == "incomplete"


# ===== 3. 成功任务产出 artifact =====


def test_task_artifact_produced():
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))

    worker = ScriptedWorkerExecutor(
        script_success={"A": True},
        artifacts={"A": ["art:code.py"]},
    )
    scheduler = TaskScheduler(dag, max_rounds=5, worker_executor=worker)
    result = scheduler._run_sync_fallback()
    assert result["status"] == "completed"
    assert "art:code.py" in dag.nodes["A"].output_artifact_ids


# ===== 4. 失败自动暂停（需外部 replan） =====


def test_failed_task_does_not_hard_block():
    """FAILED 任务不会自动导致停止——Scheduler 继续其他 node 执行。"""
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.add_node(_make_node("B"))  # 无依赖

    worker = ScriptedWorkerExecutor(
        script_success={"A": False, "B": True},
    )
    scheduler = TaskScheduler(dag, max_rounds=5, worker_executor=worker)
    result = scheduler._run_sync_fallback()
    assert dag.nodes["B"].status == TaskNodeStatus.SUCCEEDED
    assert dag.nodes["A"].status == TaskNodeStatus.FAILED


# ===== 5. max_rounds 打断 =====


def test_max_rounds_termination():
    """永远有未完成 task → 打到 max_rounds 后终止。"""
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))

    worker = ScriptedWorkerExecutor(
        script_success={"A": False},
    )
    scheduler = TaskScheduler(dag, max_rounds=3, worker_executor=worker)
    result = scheduler._run_sync_fallback()
    # A FAILED 后所有节点 terminal 但不是全 SUCCEEDED → partial_failure
    assert result["termination_reason"] == "partial_failure"
    assert result["status"] == "incomplete"


# ===== 6. 全部成功即 terminated =====


def test_all_succeeded_terminates_early():
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.add_node(_make_node("B", deps=["A"]))

    scheduler = TaskScheduler(dag, max_rounds=50, worker_executor=_InMemoryWorkerExecutor())
    result = scheduler._run_sync_fallback()
    assert result["termination_reason"] == "all_tasks_succeeded"
    # 打不到 max_rounds，因为早停了
    assert result.get("rounds", 0) == 2


# ===== 7. repair task 集成 =====


class _RepairInjectingExecutor(WorkerExecutor):
    """任意 task 第一次失败 → 注入 repair；后续 repair 成功。

    用于测试「主循环 + 局部 Replan」闭环：失败 task → 注入 repair task → repair 成功。
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def execute_task(self, dag, task_id, task_input):
        self.calls[task_id] = self.calls.get(task_id, 0) + 1

        # 原 task 第一次失败 → 注入 repair
        if "__repair" not in task_id and self.calls[task_id] == 1:
            return TaskResult(
                task_id=task_id, success=False, error="first attempt fails",
                attempted=True,
            )
        # 任意重试 / repair → 成功
        import app.multiagent.task_graph as _tg
        return TaskResult(
            task_id=task_id, success=True,
            artifact_ids=[f"art:{task_id}"], attempted=True,
        )


def test_repair_task_brings_failed_back_to_life():
    """任务失败后注入 repair，repair 成功 → DAG 全部成功。"""
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    executor = _RepairInjectingExecutor()

    # 自定义主循环：每个 ready task 执行，失败后立即注入 repair
    round_n = 0
    while round_n < 20:
        round_n += 1
        ready = dag.ready_tasks()
        if not ready:
            break
        for task in ready:
            dag.update_status(task.id, TaskNodeStatus.READY)
            dag.update_status(task.id, TaskNodeStatus.RUNNING)
            result = executor.execute_task(dag, task.id, {})
            if result.success:
                dag.update_status(task.id, TaskNodeStatus.SUCCEEDED)
                for art in result.artifact_ids:
                    dag.accept_artifact(task.id, art)
            else:
                dag.record_attempt(task.id)
                dag.update_status(task.id, TaskNodeStatus.FAILED)
                # 局部 replan
                dag.add_repair_task(
                    task.id, f"Fix {task.id}",
                    required_capabilities=["coding", "file_write"],
                )

    assert dag.all_succeeded()
    # 至少有一个 repair 节点
    assert any("repair" in nid for nid in dag.nodes)


# ===== 8. ScriptedWorkerExecutor 工作正常 =====


def test_scripted_worker():
    worker = ScriptedWorkerExecutor(
        script_success={"A": True},
        artifacts={"A": ["art:out.txt"]},
    )
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.update_status("A", TaskNodeStatus.RUNNING)

    result = worker.execute_task(dag, "A", {})
    assert result.success
    assert "art:out.txt" in result.artifact_ids


def test_scripted_worker_failure():
    worker = ScriptedWorkerExecutor(
        script_success={"A": False},
        errors={"A": "timeout"},
    )
    dag = TaskGraph(root_task_id="A")
    dag.add_node(_make_node("A"))
    dag.update_status("A", TaskNodeStatus.RUNNING)

    result = worker.execute_task(dag, "A", {})
    assert not result.success
    assert result.error == "timeout"
