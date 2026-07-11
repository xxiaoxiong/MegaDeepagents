"""Req 8：未知 Agent 路由策略与确定性别名映射测试。

验证：
- 默认拒绝未知目标（dead-letter），不广播泄漏
- 显式开启 allow_broadcast_fallback 才回退
- 别名映射确定性：精确表 + 后缀规则，不再使用 'ka in t' 模糊匹配
- 拼写错误 / 多义名 / 恶意名不被误路由
"""

from __future__ import annotations

import pytest

from app.multiagent.agent_spec import AgentSpec
from app.multiagent.bus import MessageBus, resolve_alias, EXPLICIT_ALIASES
from app.multiagent.messages import (
    AgentMessage,
    MessageType,
    MessageVisibility,
    make_message_id,
)


def _agents() -> list[AgentSpec]:
    return [
        AgentSpec(name="Planner", role="Planner", goal="g", system_prompt="p"),
        AgentSpec(name="Coder", role="Coder", goal="g", system_prompt="c"),
        AgentSpec(name="Tester", role="Tester", goal="g", system_prompt="t"),
        AgentSpec(name="ReviewerAgent", role="ReviewerAgent", goal="g", system_prompt="r"),
        AgentSpec(name="Finalizer", role="Finalizer", goal="g", system_prompt="f"),
    ]


def _msg(to_agent: str, content: str = "x") -> AgentMessage:
    return AgentMessage(
        id=make_message_id(),
        task_id="t_alias",
        room_id="r_alias",
        from_agent="Planner",
        to_agent=to_agent,
        visibility=MessageVisibility.DIRECT,
        message_type=MessageType.PLAN,
        content=content,
    )


# ===== determine-level resolve_alias =====


def test_resolve_alias_exact_match():
    assert resolve_alias("Coder", {"Coder", "Tester"}) == "Coder"


def test_resolve_alias_explicit_table():
    assert resolve_alias("DeveloperAgent", {"Coder", "Tester"}) == "Coder"
    assert resolve_alias("Developer", {"Coder", "Tester"}) == "Coder"
    assert resolve_alias("Reviewer", {"ReviewerAgent", "Coder"}) == "ReviewerAgent"


def test_resolve_alias_strip_agent_suffix():
    # TesterAgent → Tester（后缀规则）
    assert resolve_alias("TesterAgent", {"Tester", "Coder"}) == "Tester"


def test_resolve_alias_add_agent_suffix():
    """'CoderAgent' → 去掉 Agent 后缀 → 'Coder' 是预期行为。"""
    assert resolve_alias("CoderAgent", {"Coder", "Tester"}) == "Coder"


def test_resolve_alias_substring_does_not_match():
    """关键回归：'er' 不能命中 'Developer'/'Tester'。"""
    # 旧实现 'ka in t or t in ka' 会让 'er' in 'Developer' → True，错误命中
    assert resolve_alias("er", {"Coder", "Tester", "Developer"}) is None
    # 'Code' 不应命中 'Coder'（模糊子串）
    assert resolve_alias("Code", {"Coder", "Tester"}) is None
    # 'Test' 不应命中 'Tester'
    assert resolve_alias("Test", {"Coder", "Tester"}) is None


def test_resolve_alias_case_sensitive():
    """显式表区分大小写，'coder' 不应命中 'Coder'。"""
    assert resolve_alias("coder", {"Coder", "Tester"}) is None


def test_resolve_alias_malicious_long_name_does_not_leak():
    """恶意构造的 'Coder<script>' 不应通过子串匹配命中 'Coder'。"""
    assert resolve_alias("Coder<script>", {"Coder", "Tester"}) is None
    assert resolve_alias("X-Coder-Y", {"Coder", "Tester"}) is None


# ===== 端到端 MessageBus 行为 =====


def test_unknown_agent_default_rejected_to_dead_letter(tmp_path):
    bus = MessageBus(room_id="r", task_id="t", agents=_agents(),
                     allow_broadcast_fallback=False)
    msg = _msg("NonExistentAgent")
    bus.publish(msg)
    assert msg in bus._dead_letters
    assert bus._dead_letters[0].metadata.get("routing_rejected") is True
    assert "NonExistentAgent" in bus._dead_letters[0].metadata.get("unknown_agents", [])


def test_unknown_agent_fallback_to_broadcast_only_when_enabled(tmp_path):
    bus = MessageBus(room_id="r", task_id="t", agents=_agents(),
                     allow_broadcast_fallback=True)
    msg = _msg("NonExistentAgent")
    bus.publish(msg)
    # 不应进入 dead-letter
    assert len(bus._dead_letters) == 0
    # 应触发路由（虽然可能无人订阅，但不能成为 dead-letter）
    assert msg.metadata.get("routing_fallback") is True


def test_typo_target_rejected_not_broadcast():
    """'Codr'（拼错）不应通过模糊匹配命中 'Coder'，应进 dead-letter。"""
    bus = MessageBus(room_id="r", task_id="t", agents=_agents(),
                     allow_broadcast_fallback=False)
    msg = _msg("Codr")
    bus.publish(msg)
    assert len(bus._dead_letters) == 1
    assert bus._dead_letters[0].metadata.get("unknown_agents") == ["Codr"]


def test_explicit_alias_routes_correctly():
    """'DeveloperAgent' 显式映射到 'Coder'，应正常投递，不进 dead-letter。"""
    bus = MessageBus(room_id="r", task_id="t", agents=_agents(),
                     allow_broadcast_fallback=False)
    msg = _msg("DeveloperAgent", content="hello dev")
    bus.publish(msg)
    assert len(bus._dead_letters) == 0
    # to_agent 应被重写为 Coder
    assert msg.to_agent == "Coder"
    assert msg.metadata.get("alias_resolved") is True


def test_suffix_alias_routes_correctly():
    """'TesterAgent' 通过后缀规则映射到 'Tester'。"""
    bus = MessageBus(room_id="r", task_id="t", agents=_agents(),
                     allow_broadcast_fallback=False)
    msg = _msg("TesterAgent", content="test please")
    bus.publish(msg)
    assert len(bus._dead_letters) == 0
    assert msg.to_agent == "Tester"


def test_polysemous_name_does_not_leak():
    """'er' 这类多义片段不应命中任何 agent。"""
    bus = MessageBus(room_id="r", task_id="t", agents=_agents(),
                     allow_broadcast_fallback=False)
    msg = _msg("er")
    bus.publish(msg)
    assert len(bus._dead_letters) == 1
    assert "er" in bus._dead_letters[0].metadata.get("unknown_agents", [])


def test_deterministic_no_random_match():
    """多次调用同一目标结果一致，无随机性。"""
    known = {"Coder", "Tester", "ReviewerAgent"}
    for _ in range(5):
        assert resolve_alias("DeveloperAgent", known) == "Coder"
        assert resolve_alias("ATesterB", known) is None  # 旧实现可能命中
        assert resolve_alias("Reviewer", known) == "ReviewerAgent"
