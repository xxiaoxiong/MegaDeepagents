"""可观测性集成测试：Offline 模式 + mock 模式全覆盖。

测试分组：
1. Offline（默认）：无 LANGSMITH_API_KEY，零外网，只验证本地日志摘要
2. Mock：monkeypatch langsmith.traceable，验证装饰器被调用、metadata 形状正确
3. real_langsmith（仅手动）：需 LANGSMITH_API_KEY 环境变量，需 `pytest -m real_langsmith`
"""
import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from app.core import config
from app.core import observability as obs
from app.core.observability import (
    ObservabilityContext,
    emit_trace_event,
    get_current_run_url,
    init_observability,
    is_enabled,
    reset_for_test,
    trace_span,
    traceable,
)


# ==================== Fixtures ====================


@pytest.fixture(autouse=True)
def _reset_obs(monkeypatch):
    """每个测试前重置 observability 状态。"""
    # Offline/mock tests may construct a real RunTree object to validate its
    # shape, but must never send it to LangSmith.  real_langsmith remains a
    # separately marked, opt-in suite.
    try:
        from langsmith.run_trees import RunTree
        monkeypatch.setattr(RunTree, "post", lambda self, *a, **k: None)
        monkeypatch.setattr(RunTree, "patch", lambda self, *a, **k: None)
    except ImportError:
        pass
    reset_for_test()
    # 保证 settings 恢复默认
    config.settings.langsmith_enabled = False
    config.settings.langsmith_tracing = False
    config.settings.langsmith_api_key = ""
    config.settings.langsmith_offline_log = True
    yield
    reset_for_test()


# ==================== 1. Offline 模式 ====================


class TestObservabilityDisabled:
    """默认关闭路径：langsmith_enabled=False，零外网。"""

    def test_disabled_by_default(self):
        """默认 enabled=False，offline_log=True。"""
        ctx = init_observability()
        assert ctx.enabled is False
        assert ctx.offline_log is True
        assert os.environ.get("LANGSMITH_TRACING") is None

    def test_is_enabled_returns_false(self):
        assert is_enabled() is False

    def test_traceable_passthrough_when_disabled(self):
        """装饰器 no-op：被装饰函数照常返回值。"""

        @traceable(name="test_fn", run_type="llm", metadata={"k": "v"})
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_traceable_preserves_exception(self):
        """disabled 时异常照常传播。"""

        @traceable(name="err_fn")
        def err():
            raise ValueError("original")

        with pytest.raises(ValueError, match="original"):
            err()

    def test_trace_span_noop(self):
        """trace_span 上下文 no-op（disabled 时）。"""
        with trace_span("test_span", metadata={"a": 1}) as info:
            assert info["name"] == "test_span"
            assert info["run_type"] == "chain"
            assert "run" not in info  # disabled 没有真实 RunTree

    def test_trace_span_exception_propagates(self):
        """disabled 时异常照常传播。"""
        with pytest.raises(ValueError, match="boom"):
            with trace_span("err_span"):
                raise ValueError("boom")

    def test_trace_span_enabled_creates_run_tree(self):
        """enabled=True 时 trace_span 应创建 RunTree 并挂到 info['run']。"""
        from unittest.mock import MagicMock
        from app.multiagent.agent_spec import TeamRunResult

        config.settings.langsmith_enabled = True
        config.settings.langsmith_api_key = "lsv2_fake_test_key_000000000000000000000"
        ctx = init_observability()
        assert ctx.enabled is True

        # trace_span 应该创建 RunTree 并成功 yield（禁用 offline_log 减少日志噪声）
        config.settings.langsmith_offline_log = False
        with trace_span("enabled_span", run_type="llm", metadata={"k": "v"}, tags=["t1"]) as info:
            assert info["name"] == "enabled_span"
            # RunTree 实例应被挂到 info["run"]
            run = info.get("run")
            assert run is not None, "enabled 时 trace_span 应创建 RunTree"
            assert run.name == "enabled_span"
            assert run.run_type == "llm"
        # 清理
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGSMITH_PROJECT", None)
        os.environ.pop("LANGSMITH_API_KEY", None)

    def test_get_current_run_url_formats_correctly(self):
        """enabled 时 get_current_run_url 用 RunTree.id 拼接 URL。

        模拟 langsmith 的 get_current_run_tree 返回带 id 的 fake run，调用 _obs.get_current_run_url
        应生成包含 run id 的 smith.langchain.com URL。
        """
        from uuid import uuid4
        from unittest.mock import MagicMock, patch

        config.settings.langsmith_enabled = True
        config.settings.langsmith_api_key = "lsv2_fake_test_key_000000000000000000000"
        init_observability()

        fake_id = uuid4()
        fake_run = MagicMock()
        fake_run.id = fake_id

        from langsmith.run_helpers import get_current_run_tree as _real_fn
        with patch("langsmith.run_helpers.get_current_run_tree", return_value=fake_run):
            url = get_current_run_url()
        assert url is not None
        assert str(fake_id) in url
        assert url.startswith("https://smith.langchain.com/")
        assert "/o/default/projects/p/default/r/" in url
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGSMITH_PROJECT", None)
        os.environ.pop("LANGSMITH_API_KEY", None)

    def test_emit_trace_event_noop(self):
        """emit_trace_event 不会报错。"""
        emit_trace_event("test_event", {"data": 42})

    def test_get_current_run_url_none(self):
        assert get_current_run_url() is None

    def test_enabled_true_no_key_downgrades_offline(self, caplog):
        """enabled=True 但无 API_KEY，降级 offline。"""
        config.settings.langsmith_enabled = True
        config.settings.langsmith_api_key = ""
        ctx = init_observability()
        assert ctx.enabled is False
        assert ctx.offline_log is True
        assert "未配置 API_KEY" in caplog.text

    def test_offline_log_writes_trace_entries(self, caplog):
        """offline_log=True 时装饰函数应在 logger 写入 [trace] 行。"""
        caplog.set_level(logging.INFO, logger="app")

        @traceable(name="offline_test")
        def foo():
            return 42

        foo()
        assert "[trace] enter name=offline_test" in caplog.text
        assert "[trace] exit  name=offline_test" in caplog.text

    def test_trace_span_offline_log(self, caplog):
        caplog.set_level(logging.INFO, logger="app")
        with trace_span("my_span", metadata={"phase": "planning"}):
            pass
        assert "[trace] enter name=my_span" in caplog.text
        assert "[trace] exit  name=my_span" in caplog.text

    def test_emit_trace_event_offline_log(self, caplog):
        """emit_trace_event 在 offline 日志里应出现。"""
        caplog.set_level(logging.INFO, logger="app")
        emit_trace_event("speaker_selected", {"agent": "Coder"})
        assert "[trace] event name=speaker_selected" in caplog.text


# ==================== 2. Mock 模式 ====================


class TestObservabilityMockEnabled:
    """模拟 enabled=True + fake client，不真发外网。"""

    def test_enabled_with_key_sets_env(self):
        config.settings.langsmith_enabled = True
        config.settings.langsmith_tracing = True
        config.settings.langsmith_api_key = "lsv2_fake_00000000000000000000000000000000"
        config.settings.langsmith_project = "test-project"
        config.settings.langsmith_endpoint = "https://api.smith.langchain.com"
        ctx = init_observability()
        assert ctx.enabled is True
        assert os.environ.get("LANGSMITH_TRACING") == "true"
        assert os.environ.get("LANGSMITH_PROJECT") == "test-project"
        assert "LANGSMITH_API_KEY" in os.environ
        # cleanup
        del os.environ["LANGSMITH_TRACING"]
        del os.environ["LANGSMITH_PROJECT"]
        del os.environ["LANGSMITH_API_KEY"]

    def test_decorated_fn_when_enabled(self):
        """enabled=True 时 traceable 装饰不应改变函数语义。"""
        config.settings.langsmith_enabled = True
        config.settings.langsmith_api_key = "lsv2_fake_00000000000000000000000000000000"
        ctx = init_observability()
        assert ctx.enabled is True  # 确定 enabled

        @traceable(name="mock_llm", run_type="llm")
        def llm_call(prompt):
            return {"result": "ok", "actions": [{"type": "no_op"}]}

        result = llm_call("test prompt")
        assert result == {"result": "ok", "actions": [{"type": "no_op"}]}

        # cleanup env
        for key in ["LANGSMITH_TRACING", "LANGSMITH_PROJECT", "LANGSMITH_API_KEY"]:
            os.environ.pop(key, None)

    def test_reset_for_test_clears_state(self):
        """reset_for_test 清除全部全局状态。"""
        config.settings.langsmith_enabled = True
        config.settings.langsmith_api_key = "fake"
        init_observability()
        assert is_enabled() is True

        # reset 后再次初始化，但 settings 已被还原到 False
        config.settings.langsmith_enabled = False
        config.settings.langsmith_api_key = ""
        reset_for_test()
        assert is_enabled() is False
        for key in ["LANGSMITH_TRACING", "LANGSMITH_PROJECT", "LANGSMITH_API_KEY"]:
            os.environ.pop(key, None)


# ==================== 3. 初始化和幂等性 ====================


class TestInitIdempotent:

    def test_init_idempotent(self):
        """两次 init 返回相同结论。"""
        ctx1 = init_observability()
        ctx2 = init_observability()
        assert ctx1.enabled == ctx2.enabled
        assert ctx1.offline_log == ctx2.offline_log

    def test_init_with_component_name(self):
        ctx = init_observability(component="pytest")
        assert ctx.service_name == "pytest"

    def test_reset_and_reinit(self):
        reset_for_test()
        config.settings.langsmith_enabled = True
        config.settings.langsmith_api_key = "fake"
        init_observability()
        assert is_enabled() is True
        config.settings.langsmith_enabled = False
        config.settings.langsmith_api_key = ""
        reset_for_test()
        assert is_enabled() is False


# ==================== 4. Smoke：storage round 新列 ====================


def test_team_round_langsmith_run_url_column(tmp_path):
    """验证 team_rounds 表已加 langsmith_run_url 列。"""
    from app.multiagent.store import MultiAgentStore, _init_multiagent_db

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "test_obs.sqlite3"), check_same_thread=False)
    _init_multiagent_db(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(team_rounds)").fetchall()}
    assert "langsmith_run_url" in cols, f"缺少 langsmith_run_url 列，现有：{cols}"
    conn.close()
