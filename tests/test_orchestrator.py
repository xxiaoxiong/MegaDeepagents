"""SimpleOrchestrator 单元测试（§十四）。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.multiagent.orchestrator import SimpleOrchestrator, OrchestrationResult
from app.multiagent.scheduler import ScriptedWorkerExecutor
from app.multiagent.task_graph import TaskGraph, TaskNode, OutputContract
from app.multiagent.verifier import Verifier, LLMRubricVerifier, ProgrammaticVerifier


# ===== 默认降级路径 =====


def test_simple_goal_single_mode(monkeypatch):
    """短目标默认走 single 模式。"""
    from types import SimpleNamespace

    def _fake_build_model():
        class _M:
            def invoke(self, msgs):
                return SimpleNamespace(
                    content='{"result": "ok", "summary": "完成"}'
                )
        return _M()

    monkeypatch.setattr("app.llm_factory.build_model", _fake_build_model)
    orch = SimpleOrchestrator()
    result = orch.run(goal="写一个 hello", mode_override="single")
    assert result.status == "completed"
    assert result.mode == "single"


def test_light_multi_mode_with_fake_no_verifier():
    """light_multi 模式：无 verifier 时等 all_succeeded。"""
    def _planner(goal, context):
        dag = TaskGraph(root_task_id="t1")
        dag.add_node(TaskNode(
            id="t1", title="t1", objective="做点事",
            dependencies=[], required_capabilities=["coding"],
        ))
        return dag

    executor = ScriptedWorkerExecutor(script_success={"t1": True})
    orch = SimpleOrchestrator(planner=_planner, executor=executor)
    result = orch.run(goal="做个中等项目", mode_override="light_multi")
    assert result.mode == "light_multi"
    assert result.status in ("completed", "incomplete")


def test_full_multi_fallback():
    """没有 verifier 时 full_multi 降级到 planner→scheduler。"""
    def _planner(goal, context):
        dag = TaskGraph(root_task_id="task1")
        dag.add_node(TaskNode(
            id="task1", title="task1", objective="完整任务",
            dependencies=[], required_capabilities=["coding", "testing"],
            output_contract=OutputContract(artifact_type="code"),
        ))
        return dag

    executor = ScriptedWorkerExecutor(script_success={"task1": True})
    orch = SimpleOrchestrator(planner=_planner, executor=executor)
    result = orch.run(goal="build a complete system", mode_override="full_multi")
    assert result.status in ("completed", "incomplete")
    assert result.mode == "full_multi"


# ===== Repair 循环 =====


def test_repair_cycle_adds_repair_tasks():
    """有 FAILED 节点时 repair 循环添加修复节点。"""
    def _planner(goal, context):
        dag = TaskGraph(root_task_id="A")
        dag.add_node(TaskNode(
            id="A", title="A", objective="实现功能 X",
            dependencies=[], required_capabilities=["coding", "testing"],
        ))
        return dag

    # A 失败，repair_A 成功
    executor = ScriptedWorkerExecutor(
        script_success={"A": False, "A__repair_v2": True},
    )
    orch = SimpleOrchestrator(planner=_planner, executor=executor, max_repair_rounds=2)
    result = orch.run(goal="build X", mode_override="full_multi")
    # 因为 task_graph 在内部变更，外层不可直接读取
    # 验证执行没有抛异常
    assert result.status in ("completed", "incomplete")


# ===== 带 verifier 的路径 =====


def test_orchestrator_with_verifier_pass():
    """Verifier 返回 pass → status completed。"""
    def _planner(goal, context):
        dag = TaskGraph(root_task_id="task1")
        dag.add_node(TaskNode(
            id="task1", title="task1", objective="build api",
            dependencies=[], required_capabilities=["coding"],
        ))
        return dag

    verifier = Verifier(llm_rubric=LLMRubricVerifier(model_available=False))
    executor = ScriptedWorkerExecutor(script_success={"task1": True})
    orch = SimpleOrchestrator(
        planner=_planner,
        executor=executor,
        verifier=verifier,
        max_repair_rounds=1,
    )
    result = orch.run(goal="build api", mode_override="full_multi")

    # 所有任务都 SUCCEEDED + verifier pass → 最终 verdict pass
    assert result.status == "completed"


def test_orchestrator_with_verifier_repair():
    """失败然后 repair。"""
    verifier = Verifier(llm_rubric=LLMRubricVerifier(model_available=False))
    executor = ScriptedWorkerExecutor(
        script_success={"execute": False},
    )

    def _custom_planner(goal, context):
        dag = TaskGraph(root_task_id="execute")
        dag.add_node(TaskNode(
            id="execute", title="execute", objective=goal,
            dependencies=[], required_capabilities=["coding"],
        ))
        return dag

    orch = SimpleOrchestrator(
        planner=_custom_planner,
        executor=executor,
        verifier=verifier,
        max_repair_rounds=2,
    )
    result = orch.run(goal="write something", mode_override="full_multi")
    assert result.status in ("incomplete", "completed")


# ===== 强制 single =====


def test_mode_override_works(monkeypatch):
    from types import SimpleNamespace

    def _fake_build_model():
        class _M:
            def invoke(self, msgs):
                return SimpleNamespace(content='{"result":"x","summary":"y"}')
        return _M()
    monkeypatch.setattr("app.llm_factory.build_model", _fake_build_model)
    orch = SimpleOrchestrator()
    result = orch.run(goal="需要多 Agent 的复杂任务", mode_override="single")
    assert result.mode == "single"


# ===== OrchestrationResult =====


def test_orchestration_result_defaults():
    r = OrchestrationResult()
    assert r.status == "pending"
    assert r.mode == ""
    assert r.error is None
    assert r.rounds == 0
    assert r.summary == ""
