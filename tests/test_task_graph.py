"""TaskGraph 单元测试。

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
    capability_timeout,
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


# ===== 5b. Repair Task 工具预算与 id 扁平化 =====
#
# 锁定两个用户报告的 repair 根因：
# (a) "Recursion limit of 44 reached" on 1__repair_v8：repair_max_tool_calls
#     默认 40 + 旧公式 max_tool_calls*2+4=44 卡死。新地下限 60。
# (b) repair-of-repair 产生嵌套 id（1__repair_v7__repair_v9），使
#     __repair_v 子串检查与审计困难；现扁平化为 1__repair_v9。


def test_repair_task_default_tool_budget_floor_is_80():
    """target 默认 max_tool_calls=40 时，repair 取下限 80，避免 recursion_limit=200。

    旧下限 60 → recursion_limit=200，但 run_e705290b97cf4a14 的
    task_3__repair_v13/v23 在读取 10+ 文件并写修复时仍超 200。
    提升到 80 → recursion_limit=260，给读密集型修复留足空间。
    """
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    _succeed(graph, "A")
    _fail(graph, "B")
    repair = graph.add_repair_task("B", "fix", required_capabilities=["coding"])
    assert repair.budget.max_tool_calls >= 80
    # 默认（target=40）时正好取 80
    assert repair.budget.max_tool_calls == 80


def test_repair_task_inherits_higher_budget_from_target():
    """target 有更大 max_tool_calls 时继承之，仍不低于 80。"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    big = TaskNode(
        id="B", title="B", dependencies=["A"],
        budget=TaskBudget(max_tool_calls=100, max_attempts=4),
    )
    graph.add_node(big)
    _succeed(graph, "A")
    _fail(graph, "B")
    repair = graph.add_repair_task("B", "fix")
    assert repair.budget.max_tool_calls == 100  # 继承 target 的 100


def test_repair_task_inherits_capability_timeout():
    """repair 继承 target 的 max_seconds，避免 planning repair 被 300s 默认杀。"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    planning_node = TaskNode(
        id="B", title="B", dependencies=["A"],
        required_capabilities=["planning"],
        budget=TaskBudget(max_seconds=900.0, max_attempts=4),
    )
    graph.add_node(planning_node)
    _succeed(graph, "A")
    _fail(graph, "B")
    repair = graph.add_repair_task("B", "fix planning")
    assert repair.budget.max_seconds == 900.0


def test_repair_of_repair_flattens_id():
    """再修复 repair task 时 id 扁平：B__repair_v7 → B__repair_v9，而非嵌套。"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    _succeed(graph, "A")
    _fail(graph, "B")
    repair1 = graph.add_repair_task("B", "first fix", required_capabilities=["coding"])
    # repair1 id 形如 B__repair_vN，自身不含嵌套
    assert repair1.id.startswith("B__repair_v")
    assert repair1.id.count("__repair_v") == 1

    # 让 repair1 失败后再次修复（模拟 repair task 自身验证失败 → 再修复）
    _fail(graph, repair1.id)
    repair2 = graph.add_repair_task(repair1.id, "second fix", required_capabilities=["coding"])
    # 扁平化：repair2 id 仍以 B__repair_v 开头，只出现一次 __repair_v
    assert repair2.id.startswith("B__repair_v")
    assert repair2.id.count("__repair_v") == 1
    # 与 repair1 不同（版本号递增）
    assert repair2.id != repair1.id
    # repair2 仍继承 B 的上游依赖
    assert repair2.dependencies == ["A"]


def test_repair_of_repair_does_not_produce_nested_id():
    """明确回归：嵌套 id 形态 B__repair_v3__repair_v5 必须不出现。"""
    graph = TaskGraph(root_task_id="A")
    graph.add_node(_make_node("A"))
    graph.add_node(_make_node("B", deps=["A"]))
    _succeed(graph, "A")
    _fail(graph, "B")
    r1 = graph.add_repair_task("B", "fix1")
    _fail(graph, r1.id)
    r2 = graph.add_repair_task(r1.id, "fix2")
    _fail(graph, r2.id)
    r3 = graph.add_repair_task(r2.id, "fix3")
    # 三层修复后 id 仍是 B__repair_vN，不出现 __repair_v...__repair_v
    for rid in (r1.id, r2.id, r3.id):
        assert rid.count("__repair_v") == 1
        assert rid.startswith("B__repair_v")


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


# ===== 10. capability_timeout：planning 任务需更长超时 =====
#
# 锁定 run_fdad04073bf748f8 暴露的过程问题：planning 任务（"构建一个前后端项目"
# 架构设计）在 agnes 端点 900s 内无法完成，触发重试。planning 超时需 > 900s。


def test_capability_timeout_planning_above_900s():
    """planning 超时必须 > 900s，避免复杂架构设计任务被误杀。"""
    assert capability_timeout(["planning"]) > 900.0
    assert capability_timeout(["planning"]) == 1200.0


def test_capability_timeout_picks_first_primary_cap():
    """多能力时取第一个有超时映射的主角色。"""
    # coding was bumped 600→900 (multi-file projects need more time,
    # run_72df9e9852a64998 observed 19+ file writes) and testing 300→600
    # (task_4__repair_v29 timed out at 300s while authoring+running e2e
    # tests in run_3fb3c2572f1348b0).  Keep these locked to the current
    # CAPABILITY_TIMEOUTS values so future regressions are caught.
    assert capability_timeout(["coding", "file_read"]) == 900.0
    assert capability_timeout(["testing"]) == 600.0


def test_capability_timeout_unknown_caps_returns_zero():
    """无主角色能力时返回 0.0，由调度器回退到全局默认。"""
    assert capability_timeout(["file_read", "file_write"]) == 0.0
    assert capability_timeout([]) == 0.0
    assert capability_timeout(None) == 0.0
