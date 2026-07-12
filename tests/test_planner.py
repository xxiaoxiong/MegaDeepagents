"""Structured Planner 单元测试（§六）。

覆盖：
1. _llm_plan_to_taskgraph 把 JSON 转换为合法 TaskGraph
2. validate_plan 检测环、未知能力
3. plan_with_llm mock 路径
4. build_fallback_plan 降级
5. 非法环形依赖被拒绝
"""
from __future__ import annotations

import json

import pytest

from app.multiagent.planner import (
    _llm_plan_to_taskgraph,
    validate_plan,
    PlanValidationError,
    build_fallback_plan,
    plan_with_llm,
)
from app.multiagent.task_graph import TaskGraph, TaskNode, TaskNodeStatus


# ===== _llm_plan_to_taskgraph =====


def test_simple_two_step_plan():
    """一个简单的两步骤计划。"""
    json_output = {
        "tasks": [
            {
                "id": "design",
                "title": "设计 API",
                "objective": "设计 REST API",
                "dependencies": [],
                "required_capabilities": ["planning"],
                "output_artifact_type": "document",
                "acceptance_criteria": ["至少 3 个端点"],
                "priority": 10,
            },
            {
                "id": "implement",
                "title": "实现 API",
                "objective": "实现设计好的 API",
                "dependencies": ["design"],
                "required_capabilities": ["coding"],
                "output_artifact_type": "code",
                "acceptance_criteria": ["测试通过"],
                "priority": 5,
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="build API")
    assert len(graph.nodes) == 2
    assert "design" in graph.nodes
    assert "implement" in graph.nodes
    assert graph.nodes["implement"].dependencies == ["design"]
    assert graph.root_task_id == "design"  # 无依赖且 priority 最高


def test_parallel_tasks():
    json_output = {
        "tasks": [
            {"id": "a", "dependencies": [], "priority": 5,
             "required_capabilities": ["coding"]},
            {"id": "b", "dependencies": [], "priority": 5,
             "required_capabilities": ["testing"]},
            {"id": "c", "dependencies": ["a", "b"], "priority": 5,
             "required_capabilities": ["default"]},
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="g")
    assert not graph.has_cycle()
    ready = graph.ready_tasks()
    ready_ids = {n.id for n in ready}
    assert "a" in ready_ids
    assert "b" in ready_ids
    assert "c" not in ready_ids


def test_duplicate_id_raises():
    json_output = {
        "tasks": [
            {"id": "x", "dependencies": [], "required_capabilities": ["coding"]},
            {"id": "x", "dependencies": [], "required_capabilities": ["coding"]},
        ]
    }
    with pytest.raises(PlanValidationError, match="重复"):
        _llm_plan_to_taskgraph(json_output, "")


def test_missing_id_raises():
    json_output = {"tasks": [{"dependencies": []}]}
    with pytest.raises(PlanValidationError, match="缺少 id"):
        _llm_plan_to_taskgraph(json_output, "")


def test_dangling_dep_raises():
    json_output = {
        "tasks": [
            {"id": "a", "dependencies": ["nonexistent"],
             "required_capabilities": ["coding"]},
        ]
    }
    with pytest.raises(PlanValidationError, match="依赖不存在的"):
        _llm_plan_to_taskgraph(json_output, "")


def test_no_tasks_raises():
    json_output = {"tasks": []}
    with pytest.raises(PlanValidationError, match="不包含 tasks"):
        _llm_plan_to_taskgraph(json_output, "")


# ===== validate_plan =====


def test_validate_cycle_raises():
    graph = TaskGraph(root_task_id="a")
    graph.add_node(TaskNode(id="a", title="a", dependencies=["b"],
                            required_capabilities=["coding"]))
    graph.add_node(TaskNode(id="b", title="b", dependencies=["a"],
                            required_capabilities=["coding"]))
    with pytest.raises(PlanValidationError, match="存在环"):
        validate_plan(graph)


def test_validate_valid_dag_passes():
    graph = TaskGraph(root_task_id="a")
    graph.add_node(TaskNode(id="a", title="a", dependencies=[],
                            required_capabilities=["coding"]))
    graph.add_node(TaskNode(id="b", title="b", dependencies=["a"],
                            required_capabilities=["testing"]))
    # should not raise
    validate_plan(graph)


def test_validate_unknown_capability_logs_warning_only(caplog):
    import logging
    caplog.set_level(logging.WARNING)
    graph = TaskGraph(root_task_id="a")
    graph.add_node(TaskNode(id="a", title="a", dependencies=[],
                            required_capabilities=["magic_power"]))
    validate_plan(graph)  # should not raise
    assert any("magic_power" in rec.message for rec in caplog.records)


# ===== plan_with_llm mock =====


class _MockPlannerLLM:
    def __init__(self, response: str | None = None, fail_count: int = 0):
        self._response = response
        self._fail_count = fail_count
        self._calls = 0

    def bind(self, response_format=None):
        return _MockBoundLLM(self._response, self._fail_count, ref=self)


class _MockBoundLLM:
    def __init__(self, response, fail_count, ref):
        self._response = response or json.dumps({"tasks": [
            {"id": "task1", "objective": "实现 X", "dependencies": [],
             "required_capabilities": ["coding"]}
        ]})
        self._fail_count = fail_count
        self._ref = ref

    def invoke(self, messages):
        from types import SimpleNamespace
        self._ref._calls += 1
        if self._ref._calls <= self._fail_count:
            raise RuntimeError("LLM unavailable")
        content = self._response
        if callable(content):
            content = content(messages)
        return SimpleNamespace(content=content)


def test_plan_with_llm_mock():
    llm = _MockPlannerLLM()
    graph = plan_with_llm("build something", llm=llm)
    assert len(graph.nodes) == 1
    assert "task1" in graph.nodes


def test_plan_with_llm_retry_then_succeeds():
    llm = _MockPlannerLLM(fail_count=1)
    graph = plan_with_llm("do it", llm=llm)
    assert len(graph.nodes) == 1
    assert llm._calls == 2  # 1 fail + 1 success


def test_plan_with_llm_all_retries_exhausted():
    llm = _MockPlannerLLM(fail_count=10)
    with pytest.raises(PlanValidationError, match="全部失败"):
        plan_with_llm("do it", max_retries=2, llm=llm)
    assert llm._calls == 3  # initial + 2 retries = 3


# ===== build_fallback_plan =====


def test_fallback_plan_has_two_steps():
    graph = build_fallback_plan("build a web service")
    assert len(graph.nodes) == 2
    assert "plan" in graph.nodes
    assert "execute" in graph.nodes
    assert graph.nodes["execute"].dependencies == ["plan"]
    assert not graph.has_cycle()


def test_fallback_plan_can_be_scheduled():
    graph = build_fallback_plan("do x")
    assert not graph.has_cycle()
    ready = graph.ready_tasks()
    assert any(n.id == "plan" for n in ready)
    assert not any(n.id == "execute" for n in ready)


def test_fallback_root_task_id_set():
    graph = build_fallback_plan("test")
    assert graph.root_task_id == "plan"
