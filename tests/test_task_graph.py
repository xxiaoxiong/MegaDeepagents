"""TaskGraph 单元测试（docs/upgradePhaseTwo.md 测试要求 1-4）。

覆盖：
1. Planner 生成合法 DAG（add_node + validate）
2. 非法环形依赖被拒绝（has_cycle / validate 抛错）
3. 两个无依赖 Task 一起进入 ready_tasks
4. 有依赖 Task 不会提前执行（dependency 未满足时不在 ready_tasks 中）
5. 状态合法转换正常生效，非法转换被拒绝
6. Repair Task 动态新增不破坏原图
7. 拓扑排序与依赖顺序一致
8. TaskGraph 版本化自增
"""
from __future__ import annotations

import pytest as _pytest

from app.multiagent.task_graph import (
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskBudget,
    OutputContract,
    ExecutionError,
    is_legal_transition,
)


# ===== 辅助 =====


def _make_node(
    _id: str,
    deps: list[str] | None = None,
    caps: list[str] | None = None,
    priority: int = 0,
) -> TaskNode:
    return TaskNode(
        id=_id,
        title=_id,
        objective=f"do {_id}",
        dependencies=deps or [],
        required_capabilities=caps or [],
        priority=priority,
    )


def _succeed(graph: TaskGraph, node_id: str) -> None:
    """走完整合法路径把节点置为 SUCCEEDED（PENDING→READY→RUNNING→SUCCEEDED）。"""
    assert graph.update_status(node_id, TaskNodeStatus.READY)
    assert graph.update_status(node_id, TaskNodeStatus.RUNNING)
    assert graph.update_status(node_id, TaskNodeStatus.SUCCEEDED)


def _fail(graph: TaskGraph, node_id: str) -> None:
    """走完整合法路径把节点置为 FAILED。"""
    assert graph.update_status(node_id, TaskNodeStatus.READY)
    assert graph.update_status(node_id, TaskNodeStatus.RUNNING)
    assert graph.update_status(node_id, TaskNodeStatus.FAILED)


# ===== 1. 合法 DAG 校验 =====


def test_add_node_and_validate():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    graph.add_node(_make_node("C", deps=["A"]))
    graph.validate()  # 不应抛

    assert graph.version >= 1
    assert len(graph.nodes) == 3


def test_validate_raises_dangling_dep():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["NONEXISTENT"]))
    with _pytest.raises(ValueError, match="NONEXISTENT"):
        graph.validate()


def test_no_self_dependency():
    with _pytest.raises(ValueError):
        TaskNode(id="X", title="X", dependencies=["X"])


# ===== 2. 环检测 =====


def test_detect_simple_cycle():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A", deps=["B"]))
    graph.add_node(_make_node("B", deps=["C"]))
    graph.add_node(_make_node("C", deps=["A"]))
    # A → B → C → A: 1-2-3 均形成环
    assert graph.has_cycle()


def test_detect_no_cycle():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    graph.add_node(_make_node("C", deps=["A"]))
    graph.add_node(_make_node("D", deps=["B", "C"]))
    assert not graph.has_cycle()


def test_detect_single_node_dag():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    assert not graph.has_cycle()


# ===== 3. ready_tasks =====


def test_ready_tasks_parallel():
    """两个无依赖 Task 一起在 ready_tasks 中。"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=[]))
    ready = graph.ready_tasks()
    ready_ids = {n.id for n in ready}
    assert "A" in ready_ids
    assert "B" in ready_ids


def test_dependency_not_ready():
    graph = TaskGraph(root_task_id="A")
    a = _make_node("A")
    b = _make_node("B", deps=["A"])
    graph.add_node(a)
    graph.add_node(b)
    # A 尚未 SUCCEEDED，B 不应 ready
    ready = graph.ready_tasks()
    ready_ids = {n.id for n in ready}
    assert "A" in ready_ids  # A 无依赖
    assert "B" not in ready_ids


def test_dependency_satisfied_then_ready():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    _succeed(graph, "A")
    ready = graph.ready_tasks()
    ready_ids = {n.id for n in ready}
    assert "A" not in ready_ids  # A 已经 SUCCEEDED，不再可调度
    assert "B" in ready_ids


# ===== 4. 状态转换 =====


def test_illegal_transition_rejected():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.update_status("A", TaskNodeStatus.READY)
    graph.update_status("A", TaskNodeStatus.RUNNING)
    graph.update_status("A", TaskNodeStatus.SUCCEEDED)
    # SUCCEEDED → READY 非法（已终止）
    assert not graph.update_status("A", TaskNodeStatus.READY)
    assert graph.nodes["A"].status == TaskNodeStatus.SUCCEEDED  # 不变


def test_straight_path():
    """PENDING → READY → RUNNING → SUCCEEDED"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.update_status("A", TaskNodeStatus.READY)
    assert graph.nodes["A"].status == TaskNodeStatus.READY
    graph.update_status("A", TaskNodeStatus.RUNNING)
    assert graph.nodes["A"].status == TaskNodeStatus.RUNNING
    assert graph.nodes["A"].started_at is not None
    graph.update_status("A", TaskNodeStatus.SUCCEEDED)
    assert graph.nodes["A"].status == TaskNodeStatus.SUCCEEDED
    assert graph.nodes["A"].completed_at is not None


def test_fail_and_retry():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.update_status("A", TaskNodeStatus.READY)
    graph.update_status("A", TaskNodeStatus.RUNNING)
    graph.record_attempt("A", ExecutionError(code="timeout", message="LLM timeout"))
    graph.update_status("A", TaskNodeStatus.FAILED)
    assert graph.nodes["A"].attempts == 1
    assert graph.nodes["A"].error is not None

    # retry: FAILED → PENDING → READY
    graph.update_status("A", TaskNodeStatus.PENDING)
    assert graph.nodes["A"].status == TaskNodeStatus.PENDING


# ===== 5. Repair Task =====


def test_add_repair_task():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    _succeed(graph, "A")
    _fail(graph, "B")

    repair = graph.add_repair_task("B", "fix the bug", required_capabilities=["coding"])
    assert repair.id not in ("A", "B")
    # repair 继承 B 的 dependencies = ["A"]（A 已 SUCCEEDED，故 repair 应 ready）
    assert repair.dependencies == ["A"]
    assert "coding" in repair.required_capabilities
    assert repair.priority == 10
    assert repair.id in graph.nodes

    # repair 不破坏原图
    graph.validate()

    # A 已 SUCCEEDED，repair 应 ready
    ready = graph.ready_tasks()
    ready_ids = {n.id for n in ready}
    assert repair.id in ready_ids

    # 同时 B 的下游若有依赖 B 的，应改为依赖 repair
    graph.add_node(_make_node("C", deps=["B"]))
    repair2 = graph.add_repair_task("B", "fix again")
    assert "C" in graph.nodes
    # C 现在应依赖 repair2 而非 B
    assert repair2.id in graph.nodes["C"].dependencies
    assert "B" not in graph.nodes["C"].dependencies


# ===== 6. 拓扑排序 =====


def test_topological_order_respects_deps():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    graph.add_node(_make_node("C", deps=["A"]))
    graph.add_node(_make_node("D", deps=["B", "C"]))

    order = graph.topological_order()
    assert order[0] == "A"
    assert order.index("A") < order.index("B")
    assert order.index("A") < order.index("C")
    assert order.index("B") < order.index("D")
    assert order.index("C") < order.index("D")


def test_topological_order_priority():
    """高 priority 在无依赖约束下靠前。"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A", deps=[], priority=0))
    graph.add_node(_make_node("B", deps=[], priority=5))
    graph.add_node(_make_node("C", deps=[], priority=10))
    order = graph.topological_order()
    assert order.index("C") < order.index("B") < order.index("A")


# ===== 7. 版本化 =====


def test_version_increments():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    v1 = graph.version
    graph.update_status("A", TaskNodeStatus.READY)  # v2
    assert graph.version == v1 + 1


# ===== 8. 后继计算 =====


def test_descendants():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    graph.add_node(_make_node("C", deps=["A"]))
    graph.add_node(_make_node("D", deps=["B", "C"]))
    assert graph.descendants("A") == {"B", "C", "D"}
    assert graph.descendants("B") == {"D"}
    assert graph.descendants("D") == set()


def test_all_succeeded():
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    _succeed(graph, "A")
    _succeed(graph, "B")
    assert graph.all_succeeded()
    # SUCCEEDED → FAILED 是合法转换（Verifier 反查失败回退）
    assert graph.update_status("B", TaskNodeStatus.FAILED)
    assert not graph.all_succeeded()


# ===== 9. OutputContract 默认 =====


def test_default_output_contract():
    n = TaskNode(id="X", title="X")
    assert n.output_contract.allow_parallel is True
    assert n.output_contract.required_artifacts == []
