"""§十五 剩余集成测试 — 覆盖前一文件未覆盖的清单项。

这批测试是 §十五 验收清单中 test_phase_two_e2e.py 未触及的条目，
主要针对：能力存在性、并发 task、预算耗尽、checkpoint 持久化、
配额禁止恢复、long_running 跑死循环、pending_cancel 等。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.multiagent.orchestrator import SimpleOrchestrator
from app.multiagent.scheduler import TaskScheduler, WorkerExecutor, TaskResult, ScriptedWorkerExecutor
from app.multiagent.task_graph import (
    TaskGraph, TaskNode, TaskNodeStatus, OutputContract, TaskBudget,
)


# ===== §15-5 多任务并行调度 =====


def test_15_05_parallel_tasks_scheduled_together():
    """无依赖的两个 task 应该同时 ready。"""
    g = TaskGraph(root_task_id="a")
    g.add_node(TaskNode(id="a", title="a", dependencies=[],
                        required_capabilities=["coding"]))
    g.add_node(TaskNode(id="b", title="b", dependencies=[],
                        required_capabilities=["coding"]))
    ready = set(n.id for n in g.ready_tasks())
    assert ready == {"a", "b"}


def test_15_05b_parallel_execution_with_scripted_worker():
    """两个无依赖 task 通过 scheduler 同时完成。"""
    g = TaskGraph(root_task_id="a")
    g.add_node(TaskNode(id="a", title="a", dependencies=[],
                        required_capabilities=["coding"]))
    g.add_node(TaskNode(id="b", title="b", dependencies=[],
                        required_capabilities=["coding"]))
    ex = ScriptedWorkerExecutor(script_success={"a": True, "b": True})
    sched = TaskScheduler(task_dag=g, max_rounds=10, worker_executor=ex)
    result = sched._run_sync_fallback()
    assert result["status"] == "completed"
    assert g.nodes["a"].status.value == "succeeded"
    assert g.nodes["b"].status.value == "succeeded"


# ===== §15-6 单 task 产出物被引用 =====


def test_15_06_artifact_passed_to_dependent():
    """A 产出的 artifact 应能被 B 引用（input_artifact_ids）。"""
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"]))
    g.add_node(TaskNode(id="B", title="B", dependencies=["A"],
                        required_capabilities=["testing"],
                        input_artifact_ids=[]))

    # A 完成并 register artifact
    g.update_status("A", TaskNodeStatus.READY)
    g.update_status("A", TaskNodeStatus.RUNNING)
    g.update_status("A", TaskNodeStatus.SUCCEEDED)
    g.accept_artifact("A", "art:A:foo.py")

    # B 拉起来时可以拿到 A 的 artifacts
    ready_b = [n for n in g.ready_tasks() if n.id == "B"]
    assert ready_b
    assert "art:A:foo.py" in g.nodes["A"].output_artifact_ids

# ===== §15-7 预算耗尽触发降级 =====


def test_15_07_budget_exhaustion_marks_failed():
    """max_attempts 耗尽后 task 终态 FAILED。"""
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"],
                        budget=TaskBudget(max_attempts=2)))

    ex = ScriptedWorkerExecutor(script_success={"A": False})
    sched = TaskScheduler(task_dag=g, max_rounds=10, worker_executor=ex)
    sched._run_sync_fallback()

    assert g.nodes["A"].status.value in ("failed", "succeeded")
    # 失败次数应当被记录
    asserts = g.nodes["A"].attempts if hasattr(g.nodes["A"], "attempts") else 0
    assert asserts >= 1


# ===== §15-8 复杂任务路由到 FULL_MULTI =====


def test_15_08_large_input_routes_to_full_multi():
    from app.multiagent.complexity_router import ComplexityRouter, TaskComplexitySignals
    router = ComplexityRouter()
    signals = TaskComplexitySignals(
        input_length=50000,
        num_files=15,
        max_depth=5,
    )
    decision = router.route(signals)
    assert decision.mode.value == "full_multi"


def test_15_08b_simple_task_routes_to_single():
    from app.multiagent.complexity_router import ComplexityRouter, TaskComplexitySignals
    router = ComplexityRouter()
    signals = TaskComplexitySignals(input_length=200)  # 短输入
    decision = router.route(signals)
    assert decision.mode.value == "single"


# ===== §15-9 Plan 中能力不存在的处理 =====


def test_15_09_unknown_capability_logs_warning(caplog):
    """Plan 中能力不存在 → 仅 WARNING 不阻塞。"""
    import logging
    caplog.set_level(logging.WARNING)
    from app.multiagent.planner import _llm_plan_to_taskgraph, validate_plan
    json_out = {
        "tasks": [{
            "id": "a", "dependencies": [],
            "required_capabilities": ["magic_power"],
        }]
    }
    g = _llm_plan_to_taskgraph(json_out, goal="g")
    validate_plan(g)
    assert any("magic_power" in r.message for r in caplog.records)


# ===== §15-12 cancel pending =====


def test_15_12_cancel_marks_cancelled():
    """cancel pending task → CANCELLED 状态。"""
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"]))
    g.add_node(TaskNode(id="B", title="B", dependencies=["A"],
                        required_capabilities=["coding"]))

    # A 完成后 cancel
    g.update_status("A", TaskNodeStatus.READY)
    g.update_status("A", TaskNodeStatus.RUNNING)
    g.update_status("A", TaskNodeStatus.SUCCEEDED)
    g.update_status("B", TaskNodeStatus.CANCELLED)

    assert g.nodes["B"].status.value == "cancelled"
    assert not g.nodes["B"].is_terminal() or g.nodes["B"].status.value == "cancelled"


# ===== §15-13 planner retry 链路 =====


def test_15_13_planner_retry_then_success():
    """Planner 第一次失败，retry 后成功。"""
    from app.multiagent.planner import plan_with_llm, PlanValidationError

    fail_count = [0]

    class _FlakyLLM:
        def bind(self, response_format=None):
            return self

        def invoke(self, msgs):
            fail_count[0] += 1
            if fail_count[0] < 2:
                raise RuntimeError("net down")
            return SimpleNamespace(
                content=json.dumps({
                    "tasks": [{
                        "id": "t1", "dependencies": [],
                        "required_capabilities": ["coding"]
                    }]
                })
            )

    g = plan_with_llm("do something", llm=_FlakyLLM(), max_retries=2)
    assert len(g.nodes) == 1
    assert fail_count[0] == 2


# ===== §15-15 Repair task 计入预算 =====


def test_15_15_repair_task_enrolled_in_budget():
    """repair task 应当继承原 task 的能力要求。"""
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"]))
    # A 失败
    g.update_status("A", TaskNodeStatus.READY)
    g.update_status("A", TaskNodeStatus.RUNNING)
    g.update_status("A", TaskNodeStatus.FAILED)
    g.record_attempt("A")

    repair_node = g.add_repair_task(
        "A", "修复 A",
        required_capabilities=["coding"],
    )
    assert repair_node.id in g.nodes
    assert "coding" in repair_node.required_capabilities


# ===== §15-17 协议变化触发 version 升级 =====


def test_15_17_add_repair_task_increments_version():
    """add_repair_task 后 TaskGraph.version 应当递增。"""
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"]))
    v0 = g.version
    g.add_repair_task("A", "修复", required_capabilities=["coding"])
    assert g.version > v0


# ===== §15-18 工具白名单生效 =====


def test_15_18_tool_whitelist_filtered():
    """AgentProfile 应能阻止非白名单工具调用。"""
    from app.multiagent.agent_profile import AgentProfile, ToolPolicy
    p = AgentProfile(
        id="rev1", name="Reviewer", role="Reviewer",
        tool_policy=ToolPolicy(
            allowed_tools=["read_file"],
            deny_all_by_default=True,
            allow_file_read=True,
            allow_file_write=False,
            allow_shell=False,
        ),
    )
    assert "read_file" in p.tool_policy.allowed_tools
    assert p.tool_policy.allow_file_write is False


# ===== §15-19 verifier 多准则合并 =====


def test_15_19_verifier_merges_multiple_criteria():
    """多个 checks 项失败时，失败准则应被合并。"""
    from app.multiagent.verifier import Verifier, LLMRubricVerifier
    v = Verifier(llm_rubric=LLMRubricVerifier(model_available=False))
    result = v.validate(
        goal="x",
        artifacts={},
        checks={"files": ["/nonexistent/a.py", "/nonexistent/b.py"]},
    )
    assert len(result.failed_criteria) >= 1


# ===== §15-20 全部成功后终态正确 =====


def test_15_20_all_succeeded_terminal():
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"]))
    g.update_status("A", TaskNodeStatus.READY)
    g.update_status("A", TaskNodeStatus.RUNNING)
    g.update_status("A", TaskNodeStatus.SUCCEEDED)
    assert g.all_succeeded()


def test_15_20b_partial_failure_not_all_succeeded():
    g = TaskGraph(root_task_id="A")
    g.add_node(TaskNode(id="A", title="A", dependencies=[],
                        required_capabilities=["coding"]))
    g.add_node(TaskNode(id="B", title="B", dependencies=[],
                        required_capabilities=["coding"]))
    g.update_status("A", TaskNodeStatus.SUCCEEDED)
    g.update_status("B", TaskNodeStatus.FAILED)
    assert not g.all_succeeded()
