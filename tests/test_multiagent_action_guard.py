"""Agent 级工具/动作权限运行时强制隔离测试。

验证关键场景：
1. Reviewer 越权 create_artifact 被拦
2. Coder 越权 mark_done 被拦
3. Planner 越权 mark_done 被拦
4. 仅 Finalizer 可 mark_done
5. Coder 越权产出 review_result 不触发返工闭环
6. 角色默认白名单兜底（未显式声明 allowed_actions 也受保护）
7. action_guard 出口仍产出可观测的拒绝型 no_op
"""

from __future__ import annotations

from app.multiagent.action_guard import (
    DEFAULT_ROLE_ALLOWED_ACTIONS,
    filter_actions_by_permission,
    get_effective_allowed_actions,
    is_action_allowed,
)
from app.multiagent.agent_spec import AgentSpec
from app.multiagent.messages import MessageType


def _coder(allowed_actions: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        name="Coder",
        role="Coder",
        goal="写代码",
        allowed_tools=["create_file"],
        allowed_actions=allowed_actions or [],
    )


def _reviewer() -> AgentSpec:
    return AgentSpec(
        name="ReviewerAgent",
        role="ReviewerAgent",
        goal="评审",
        allowed_tools=[],
        allowed_actions=[],
    )


def _planner() -> AgentSpec:
    return AgentSpec(
        name="Planner",
        role="Planner",
        goal="规划",
        allowed_tools=[],
        allowed_actions=[],
    )


def _finalizer() -> AgentSpec:
    return AgentSpec(
        name="Finalizer",
        role="Finalizer",
        goal="收尾",
        allowed_tools=[],
        allowed_actions=[],
    )


def test_default_role_whitelist_completeness():
    """角色默认白名单覆盖 6 个核心角色。"""
    expected = {"Planner", "Coder", "Tester", "ReviewerAgent", "Finalizer", "Researcher"}
    assert expected.issubset(set(DEFAULT_ROLE_ALLOWED_ACTIONS.keys()))


def test_finalizer_default_can_mark_done():
    """Finalizer 默认白名单包含 mark_done。"""
    assert "mark_done" in DEFAULT_ROLE_ALLOWED_ACTIONS["Finalizer"]


def test_planner_default_cannot_mark_done():
    """Planner 默认白名单不包含 mark_done（防早终结）。"""
    assert "mark_done" not in DEFAULT_ROLE_ALLOWED_ACTIONS["Planner"]


def test_coder_default_cannot_review_result():
    """Coder 默认白名单不包含 respond_critique / mark_done。"""
    coder_defaults = DEFAULT_ROLE_ALLOWED_ACTIONS["Coder"]
    assert "mark_done" not in coder_defaults
    assert "respond_critique" not in coder_defaults


def test_reviewer_default_cannot_create_artifact():
    """Reviewer 默认白名单不包含 create_artifact。"""
    assert "create_artifact" not in DEFAULT_ROLE_ALLOWED_ACTIONS["ReviewerAgent"]


def test_get_effective_allowed_actions_explicit_overrides_default():
    """显式 allowed_actions 优先于 role 默认。"""
    agent = _coder(allowed_actions=["no_op"])
    assert get_effective_allowed_actions(agent) == ["no_op"]


def test_get_effective_allowed_actions_falls_back_to_default():
    """未显式 allowed_actions 时，按 role 兜底。"""
    agent = _coder()
    # 兜底应是 Coder 默认白名单
    expected = DEFAULT_ROLE_ALLOWED_ACTIONS["Coder"]
    assert get_effective_allowed_actions(agent) == expected


def test_filter_actions_blocks_reviewer_create_artifact():
    """Reviewer 越权 create_artifact → 被替换为拒绝型 no_op。"""
    agent = _reviewer()
    actions = [
        {"type": "send_message", "to_agent": "Coder", "content": "critique"},
        {"type": "create_artifact", "artifact_path": "/x.py"},
    ]
    filtered = filter_actions_by_permission(agent, actions)
    assert len(filtered) == 2
    # send_message 应原样保留
    assert filtered[0]["type"] == "send_message"
    # create_artifact 被替换为 no_op 并附带拒绝信息
    assert filtered[1]["type"] == "no_op"
    assert filtered[1].get("rejected_action_type") == "create_artifact"


def test_filter_actions_blocks_coder_mark_done():
    """Coder 越权 mark_done → 被替换为 no_op。"""
    agent = _coder()
    actions = [
        {"type": "create_artifact", "artifact_path": "/main.py"},
        {"type": "mark_done", "content": "完成了"},
    ]
    filtered = filter_actions_by_permission(agent, actions)
    # create_artifact 允许，mark_done 拒绝
    assert filtered[0]["type"] == "create_artifact"
    assert filtered[1]["type"] == "no_op"
    assert filtered[1].get("rejected_action_type") == "mark_done"


def test_filter_actions_blocks_planner_mark_done():
    """Planner 越权 mark_done 被阻拦（防 Planner 早终结是修复复测的核心场景）。"""
    agent = _planner()
    actions = [
        {"type": "send_message", "message_type": "plan", "content": "计划..."},
        {"type": "mark_done", "content": "结束"},
    ]
    filtered = filter_actions_by_permission(agent, actions)
    assert filtered[0]["type"] == "send_message"
    assert filtered[1]["type"] == "no_op"
    assert filtered[1].get("rejected_action_type") == "mark_done"


def test_filter_actions_allows_finalizer_mark_done():
    """Finalizer 正常产出 mark_done，不被拦。"""
    agent = _finalizer()
    actions = [
        {"type": "send_message", "message_type": "final", "content": "最终回答"},
        {"type": "mark_done", "content": "完成"},
    ]
    filtered = filter_actions_by_permission(agent, actions)
    assert filtered == actions


def test_is_action_allowed_helper():
    """is_action_allowed 单 action 判断。"""
    assert is_action_allowed(_finalizer(), "mark_done") is True
    assert is_action_allowed(_coder(), "mark_done") is False
    assert is_action_allowed(_planner(), "send_message") is True
    assert is_action_allowed(_reviewer(), "create_artifact") is False


def test_filter_actions_empty_when_no_default_role():
    """未在默认表里、未显式声明 allowed_actions 的角色 = 不限制。"""
    unknown = AgentSpec(name="X", role="UnknownRole", goal="", allowed_tools=[])
    actions = [{"type": "mark_done"}, {"type": "create_artifact"}]
    filtered = filter_actions_by_permission(unknown, actions)
    # 不限制 = 全部放行
    assert filtered == actions


def test_filter_actions_preserves_order():
    """被拦截动作位置保持原序，不被丢弃压缩。"""
    agent = _reviewer()
    actions = [
        {"type": "request_review"},
        {"type": "create_artifact"},  # 被拦
        {"type": "send_message"},
    ]
    filtered = filter_actions_by_permission(agent, actions)
    assert len(filtered) == 3
    assert filtered[0]["type"] == "request_review"
    assert filtered[1]["type"] == "no_op"
    assert filtered[2]["type"] == "send_message"
