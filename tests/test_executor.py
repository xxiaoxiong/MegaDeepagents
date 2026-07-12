"""AgentExecutor 单元测试（§三）。

覆盖：
- ModelDecisionExecutor 占位逻辑（mock build_model）
- DeepAgentExecutor 受限工具集构建与权限过滤
- create_executor 决策路由
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.multiagent.agent_profile import (
    AgentProfile,
    ModelPolicy,
    ToolPolicy,
)
from app.multiagent.executor import (
    AgentExecutionResult,
    DeepAgentExecutor,
    ExecutionContext,
    ModelDecisionExecutor,
    TaskAssignment,
    _build_boundary_prompt,
    _build_restricted_tools,
    create_executor,
)


# ===== Data classes =====


def test_task_assignment_defaults():
    a = TaskAssignment(task_id="t1", objective="写一个 API", description="实现 /hello")
    assert a.input_artifact_ids == []
    assert a.max_attempts == 2
    assert a.metadata == {}


def test_execution_context_defaults():
    ctx = ExecutionContext(run_id="r1", workspace_root="/tmp/ws")
    assert ctx.task_dag is None
    assert ctx.langsmith_trace_id is None


def test_execution_result_default_fields():
    r = AgentExecutionResult(success=True)
    assert r.output_summary == ""
    assert r.produced_artifact_ids == []
    assert r.tool_calls == []
    assert r.error is None


# ===== 受限工具构建 =====


def test_build_restricted_tools_deny_default_with_no_allowed():
    tools = _build_restricted_tools(
        allowed_tools=[],
        deny_default=True,
        task_workspace="/tmp/ws",
    )
    assert tools == [], "deny_default=True 且无 allowed_tools 应无工具"


def test_build_restricted_tools_deny_default_with_only_read():
    tools = _build_restricted_tools(
        allowed_tools=["read_file"],
        deny_default=True,
        task_workspace="/tmp",
    )
    names = [t.name for t in tools]
    assert names == ["read_file"]


def test_build_restricted_tools_coder_profile():
    """Coder 拥有 create_file/edit_file/execute/read_file/list_dir。"""
    tools = _build_restricted_tools(
        allowed_tools=["create_file", "edit_file", "execute", "read_file", "list_dir"],
        deny_default=True,
        task_workspace="/tmp/coder",
    )
    names = sorted(t.name for t in tools)
    assert "create_file" in names
    assert "edit_file" in names
    assert "execute" in names
    assert "read_file" in names
    assert "list_dir" in names


def test_build_restricted_tools_tester_profile_no_create_file():
    """Tester profile 不允许 create_file/edit_file（按我们的 default profile）。"""
    # default tester: allowed_tools=["execute", "read_file", "create_file", "list_dir"]
    tools = _build_restricted_tools(
        allowed_tools=["execute", "read_file", "list_dir"],
        deny_default=True,
        task_workspace="/tmp",
    )
    names = sorted(t.name for t in tools)
    assert names == ["execute", "list_dir", "read_file"]


def test_build_restricted_tools_reviewer_profile_readonly():
    """Reviewer 只有 read_file/list_dir。"""
    tools = _build_restricted_tools(
        allowed_tools=["read_file", "list_dir"],
        deny_default=True,
        task_workspace="/tmp",
    )
    names = sorted(t.name for t in tools)
    assert names == ["list_dir", "read_file"]


def test_build_restricted_tools_deny_default_off_all_enabled():
    """deny_default=False 时全开。"""
    tools = _build_restricted_tools(
        allowed_tools=[],
        deny_default=False,
        task_workspace="/tmp",
    )
    names = {t.name for t in tools}
    assert "read_file" in names
    assert "create_file" in names
    assert "execute" in names


# ===== Boundary prompt =====


def test_build_boundary_prompt_includes_permissions():
    profile = AgentProfile(
        id="coder1", name="Coder", role="Coder",
        tool_policy=ToolPolicy(
            allowed_tools=["create_file", "edit_file"],
            deny_all_by_default=True,
            allow_file_read=True,
            allow_file_write=True,
            allow_shell=True,
        ),
    )
    bp = _build_boundary_prompt(profile)
    assert "create_file" in bp
    assert "文件读取：允许" in bp
    assert "文件写入：允许" in bp
    assert "Shell执行：允许" in bp


def test_build_boundary_prompt_reviewer_readonly():
    profile = AgentProfile(
        id="rev1", name="Reviewer", role="Reviewer",
        tool_policy=ToolPolicy(
            allowed_tools=["read_file"],
            deny_all_by_default=True,
            allow_file_read=True,
            allow_file_write=False,
            allow_shell=False,
        ),
    )
    bp = _build_boundary_prompt(profile)
    assert "文件写入：禁止" in bp
    assert "Shell执行：禁止" in bp


# ===== create_executor 路由 =====


def test_create_executor_routes_reviewer_to_decision():
    """Reviewer 只读 → ModelDecisionExecutor。"""
    profile = AgentProfile(
        id="rev1", name="Reviewer", role="Reviewer",
        tool_policy=ToolPolicy(
            allowed_tools=["read_file"],
            deny_all_by_default=True,
            allow_file_read=True,
            allow_file_write=False,
            allow_shell=False,
        ),
    )
    ex = create_executor(profile)
    assert isinstance(ex, ModelDecisionExecutor)


def test_create_executor_routes_coder_to_deep_agent():
    """Coder 有 file_write → DeepAgentExecutor。"""
    profile = AgentProfile(
        id="coder1", name="Coder", role="Coder",
        tool_policy=ToolPolicy(
            allowed_tools=["create_file", "edit_file", "execute", "read_file"],
            deny_all_by_default=True,
            allow_file_read=True,
            allow_file_write=True,
            allow_shell=True,
        ),
    )
    ex = create_executor(profile)
    assert isinstance(ex, DeepAgentExecutor)


def test_create_executor_routes_tester_to_deep_agent_with_shell():
    """Tester 有 shell → DeepAgentExecutor。"""
    profile = AgentProfile(
        id="t1", name="Tester", role="Tester",
        tool_policy=ToolPolicy(
            allowed_tools=["execute", "read_file"],
            deny_all_by_default=True,
            allow_file_read=True,
            allow_file_write=False,
            allow_shell=True,
        ),
    )
    ex = create_executor(profile)
    assert isinstance(ex, DeepAgentExecutor)


def test_create_executor_routes_planner_with_no_tools_to_decision():
    """Planner 无任何工具 → ModelDecisionExecutor。"""
    profile = AgentProfile(
        id="p1", name="Planner", role="Planner",
        tool_policy=ToolPolicy(
            allowed_tools=[],
            deny_all_by_default=True,
            allow_file_read=False,
            allow_file_write=False,
            allow_shell=False,
        ),
    )
    ex = create_executor(profile)
    assert isinstance(ex, ModelDecisionExecutor)


# ===== ModelDecisionExecutor（用 mock LLM） =====


class _MockLLM:
    def __init__(self, content):
        self._content = content

    def bind(self, response_format=None):
        return self

    def invoke(self, messages):
        # 模拟 LangChain response 对象
        from types import SimpleNamespace
        return SimpleNamespace(content=self._content)


def test_model_decision_executor_success(monkeypatch):
    """Mock LLM 返回合规 JSON → success=True。"""
    mock_content = json.dumps({
        "decision": "approve",
        "reasoning": "all tests pass",
    })
    mock_llm = _MockLLM(mock_content)

    import app.multiagent.executor as ex_mod
    monkeypatch.setattr(
        "app.llm_factory.build_model", lambda: mock_llm
    )

    profile = AgentProfile(
        id="p1", name="Planner", role="Planner",
        tool_policy=ToolPolicy(deny_all_by_default=True, allow_file_read=False),
    )
    assignment = TaskAssignment(
        task_id="t1",
        objective="拆分任务",
        description="拆成实现、测试、评审",
        input_artifact_ids=[],
    )
    ctx = ExecutionContext(run_id="r1", workspace_root="/tmp/ws")

    executor = ModelDecisionExecutor()
    result = executor.execute(assignment, profile, ctx)
    assert result.success is True
    assert "approve" in result.output_summary


def test_model_decision_executor_handles_non_json(monkeypatch):
    """LLM 返回非 JSON → 不报错，标记 not_parsed。"""
    mock_llm = _MockLLM("这是自由文本，不是 JSON")
    monkeypatch.setattr(
        "app.llm_factory.build_model", lambda: mock_llm
    )

    profile = AgentProfile(
        id="p1", name="Planner", role="Planner",
        tool_policy=ToolPolicy(deny_all_by_default=True, allow_file_read=False),
    )
    assignment = TaskAssignment(task_id="t", objective="o", description="d")
    ctx = ExecutionContext(run_id="r", workspace_root="/tmp")

    result = ModelDecisionExecutor().execute(assignment, profile, ctx)
    assert result.success is True
    assert "llm_output_not_parsed" in result.output_summary


def test_model_decision_executor_handles_llm_exception(monkeypatch):
    """LLM 抛异常 → success=False 且带 error。"""
    def raise_exc():
        raise RuntimeError("network down")

    monkeypatch.setattr("app.llm_factory.build_model", raise_exc)

    profile = AgentProfile(
        id="p1", name="Planner", role="Planner",
        tool_policy=ToolPolicy(deny_all_by_default=True, allow_file_read=False),
    )
    assignment = TaskAssignment(task_id="t", objective="o", description="d")
    ctx = ExecutionContext(run_id="r", workspace_root="/tmp")

    result = ModelDecisionExecutor().execute(assignment, profile, ctx)
    assert result.success is False
    assert "network down" in result.error


def test_model_decision_executor_listvalue_content(monkeypatch):
    """content 是 list（部分 LangChain 模型返回）→ 通过 json.dumps 处理。"""
    mock_llm = _MockLLM([{"decision": "x"}])
    monkeypatch.setattr(
        "app.llm_factory.build_model", lambda: mock_llm
    )

    profile = AgentProfile(
        id="p1", name="Planner", role="Planner",
        tool_policy=ToolPolicy(deny_all_by_default=True, allow_file_read=False),
    )
    assignment = TaskAssignment(task_id="t", objective="o", description="d")
    ctx = ExecutionContext(run_id="r", workspace_root="/tmp")

    result = ModelDecisionExecutor().execute(assignment, profile, ctx)
    assert result.success is True


# ===== DeepAgentExecutor mock 路径 =====


def test_deep_agent_executor_mock_response():
    """通过 _mock_response 注入结果，跳过真实 LLM。"""
    executor = DeepAgentExecutor()
    mock_res = AgentExecutionResult(
        success=True,
        output_summary="hello.py 已写入",
        produced_artifact_ids=["t1:hello.py"],
        tool_calls=[{"tool": "create_file"}],
    )
    executor._mock_response = mock_res

    profile = AgentProfile(
        id="c1", name="Coder", role="Coder",
        tool_policy=ToolPolicy(
            allowed_tools=["create_file"],
            deny_all_by_default=True,
            allow_file_write=True,
        ),
    )
    assignment = TaskAssignment(
        task_id="t1", objective="写 hello", description="写一个 hello.py",
    )
    ctx = ExecutionContext(run_id="r1", workspace_root="/tmp/ws")

    result = executor.execute(assignment, profile, ctx)
    assert result is mock_res
    assert result.success is True
    assert result.output_summary == "hello.py 已写入"
    assert result.produced_artifact_ids == ["t1:hello.py"]


def test_deep_agent_executor_mock_invoke_callback():
    """通过 _mock_invoke 注入回调，验证调用参数。"""
    executor = DeepAgentExecutor()
    captured = {}

    def mock_invoke(assignment, profile, context):
        captured["task_id"] = assignment.task_id
        captured["profile_id"] = profile.id
        captured["run_id"] = context.run_id
        return AgentExecutionResult(success=True, output_summary="mocked")

    executor._mock_invoke = mock_invoke

    profile = AgentProfile(
        id="c1", name="Coder", role="Coder",
        tool_policy=ToolPolicy(allow_file_write=True),
    )
    assignment = TaskAssignment(task_id="tX", objective="x", description="x")
    ctx = ExecutionContext(run_id="rX", workspace_root="/tmp/ws")

    result = executor.execute(assignment, profile, ctx)
    assert result.success is True
    assert result.output_summary == "mocked"
    assert captured == {"task_id": "tX", "profile_id": "c1", "run_id": "rX"}


# ===== AgentExecutor 协议结构兼容性 =====


def test_both_executors_satisfy_protocol():
    """ModelDecisionExecutor + DeepAgentExecutor 都实现 execute 方法。"""
    m = ModelDecisionExecutor()
    d = DeepAgentExecutor()
    assert hasattr(m, "execute")
    assert hasattr(d, "execute")
    assert callable(m.execute)
    assert callable(d.execute)
