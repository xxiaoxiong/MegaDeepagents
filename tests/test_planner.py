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


def test_multiple_primary_capabilities_collapse_to_first(caplog):
    """LLM 偶尔会把多个主角色能力合并到同一 task 上。

    解析层会把多主角色裁剪为第一个，工具能力保留，避免 team_builder
    因找不到同时具备多角色的 worker 而让整 run failed。
    """
    json_output = {
        "tasks": [
            {
                "id": "T1",
                "dependencies": [],
                "required_capabilities": [
                    "planning", "research", "summarization", "file_write",
                ],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="g")
    node = graph.nodes["T1"]
    caps = set(node.required_capabilities)
    # 仅保留第一个主角色 + 工具能力
    assert "planning" in caps
    assert "research" not in caps
    assert "summarization" not in caps
    assert "file_write" in caps
    assert any("多个主角色能力" in r.message for r in caplog.records)


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


# ===== 启发式能力纠错：planning→coding/testing =====
#
# 锁定"我发布的任务是构建一个前后端项目，但是整个过程我只看到 planner
# 智能体在干活"根因：LLM 对构建类目标常把所有子任务标成 ["planning"]，
# TeamBuilder 只生成 Planner agent，Planner 凭 file_write 自己实现一切。
# 解析层对仅声明 planning 的 task 扫描文本：命中实现关键词→coding；
# 命中测试关键词→testing；纯规划/设计/架构保留 planning。


def test_planning_task_with_impl_keywords_becomes_coding(caplog):
    """声明 planning 但文本含实现关键词（前端/后端/构建）→ 改写为 coding。"""
    import logging
    caplog.set_level(logging.WARNING)
    json_output = {
        "tasks": [
            {
                "id": "frontend",
                "title": "构建前端项目",
                "objective": "实现前端页面与组件",
                "description": "用 Vue 构建前端",
                "dependencies": [],
                "required_capabilities": ["planning", "file_write", "shell_execute"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="构建一个前后端项目")
    caps = graph.nodes["frontend"].required_capabilities
    assert caps[0] == "coding"  # 主角色被改写
    # 工具能力保留
    assert "file_write" in caps
    assert "shell_execute" in caps
    assert "planning" not in caps
    assert any("改写主角色能力为 coding" in r.message for r in caplog.records)


def test_planning_task_with_test_keywords_becomes_testing(caplog):
    """声明 planning 但文本含测试关键词（测试/pytest）→ 改写为 testing。"""
    import logging
    caplog.set_level(logging.WARNING)
    json_output = {
        "tasks": [
            {
                "id": "tests",
                "title": "编写单元测试",
                "objective": "为后端 API 编写 pytest 测试",
                "dependencies": [],
                "required_capabilities": ["planning", "shell_execute"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="构建一个前后端项目")
    caps = graph.nodes["tests"].required_capabilities
    assert caps[0] == "testing"
    assert "shell_execute" in caps
    assert "planning" not in caps
    assert any("改写主角色能力为 testing" in r.message for r in caplog.records)


def test_planning_task_with_english_impl_keyword_becomes_coding():
    """英文实现关键词 build/implement/api 也触发改写（词边界匹配）。"""
    json_output = {
        "tasks": [
            {
                "id": "api",
                "title": "Implement backend API",
                "objective": "build the REST api service",
                "dependencies": [],
                "required_capabilities": ["planning"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="build fullstack")
    assert graph.nodes["api"].required_capabilities[0] == "coding"


def test_pure_planning_task_stays_planning():
    """纯架构/设计/规划类 task 保留 planning，不被误改。"""
    json_output = {
        "tasks": [
            {
                "id": "design",
                "title": "架构设计",
                "objective": "设计系统架构与技术选型",
                "description": "输出架构设计文档",
                "dependencies": [],
                "required_capabilities": ["planning"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="构建一个前后端项目")
    assert graph.nodes["design"].required_capabilities == ["planning"]


def test_design_task_mentions_frontend_still_stays_planning():
    """设计类 task 即使提到"前后端"（含"后端"子串）也保留 planning。

    回归锁定：早期 heuristic 用子串匹配，"设计前后端架构"因含"后端"被误判为
    coding。现 design 关键词（设计/架构/规划/方案）优先，保护架构任务。
    """
    json_output = {
        "tasks": [
            {
                "id": "arch",
                "title": "架构设计",
                "objective": "设计前后端架构与技术选型",
                "dependencies": [],
                "required_capabilities": ["planning"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="构建一个前后端项目")
    assert graph.nodes["arch"].required_capabilities == ["planning"]


def test_planning_task_with_secondary_primary_cap_still_rewritten():
    """多主角色先被裁剪为第一个 planning（coding 被丢），随后启发式仍会改写。

    裁剪后 caps=["planning"]，caps[1:] 为空，启发式条件成立；文本含实现
    关键词→最终改写为 coding。这符合用户意图：实现类任务就该由 Coder 接管。
    """
    json_output = {
        "tasks": [
            {
                "id": "mixed",
                "title": "实现前端",
                "objective": "构建前端页面",
                "dependencies": [],
                "required_capabilities": ["planning", "coding"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="build")
    # 多主角色裁剪丢掉 coding，启发式再据"实现/构建"改写为 coding
    assert graph.nodes["mixed"].required_capabilities[0] == "coding"


def test_fullstack_project_decomposition_produces_multiple_roles():
    """端到端：构建前后端项目的计划应产生 planning + coding + testing 多角色。

    这是用户报告的核心症状回归测试——之前所有 task 都是 planning 导致
    只有 Planner agent 在干活。
    """
    json_output = {
        "tasks": [
            {
                "id": "task_1",
                "title": "架构设计",
                "objective": "设计前后端架构",
                "dependencies": [],
                "required_capabilities": ["planning"],
            },
            {
                "id": "task_2",
                "title": "实现前端",
                "objective": "构建前端页面与组件",
                "dependencies": ["task_1"],
                "required_capabilities": ["planning", "file_write", "shell_execute"],
            },
            {
                "id": "task_3",
                "title": "实现后端 API",
                "objective": "开发后端接口",
                "dependencies": ["task_1"],
                "required_capabilities": ["planning", "file_write", "shell_execute"],
            },
            {
                "id": "task_4",
                "title": "编写并运行测试",
                "objective": "为前后端编写 pytest 测试",
                "dependencies": ["task_2", "task_3"],
                "required_capabilities": ["planning", "shell_execute"],
            },
        ]
    }
    graph = _llm_plan_to_taskgraph(json_output, goal="构建一个前后端项目")
    primary_caps = {
        graph.nodes[f"task_{i}"].required_capabilities[0] for i in range(1, 5)
    }
    # 应该有 planning（架构）+ coding（前端/后端）+ testing（测试）多角色
    assert "planning" in primary_caps
    assert "coding" in primary_caps
    assert "testing" in primary_caps
    # 不应全是 planning
    assert primary_caps != {"planning"}
