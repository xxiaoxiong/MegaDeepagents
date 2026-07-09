"""Agent action 权限护栏：运行时强制隔离每个角色能产出的 action 类型与能调用的工具。

设计目标：
1. 阻止 Reviewer Agent 越权改代码（不能 create_artifact）
2. 阻止 Coder 自评通过（不能产出 review_result / mark_done）
3. 阻止 Planner 直接进入 finalizing（不能 mark_done）
4. 只有 Finalizer 才能产出 mark_done（真正宣布任务完成）
5. 按 AgentSpec.allowed_tools 过滤工具白名单，避免每位 Agent 共用全部工具

这是把"角色 prompt 描述的职责边界"落到"运行时强制白名单"的关键一环。
prompt 是软约束，guard 是硬约束；二者结合才能真正实现 Agent isolation。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import AgentSpec


# ===== 角色默认 action 白名单 =====
# 当 AgentSpec.allowed_actions 为空时，按 role 名匹配以下默认白名单兜底。
# 这样既允许显式 allowed_actions 覆盖，也保证没显式配置的角色仍受基础护栏保护。
DEFAULT_ROLE_ALLOWED_ACTIONS: dict[str, list[str]] = {
    "Planner": [
        "send_message",
        "update_state",
        "handoff",
        "no_op",
        # Planner 不应 create_artifact（不应直接写代码产物）
        # 不应 request_review / respond_critique / mark_done
    ],
    "Coder": [
        "send_message",
        "create_artifact",
        "request_review",
        "handoff",
        "no_op",
        # Coder 不应 review_result / mark_done
    ],
    "Tester": [
        "send_message",
        "create_artifact",  # 创建测试用例产物
        "handoff",
        "no_op",
    ],
    "ReviewerAgent": [
        "send_message",  # critique / review_result
        "request_review",
        "respond_critique",
        "no_op",
        # Reviewer 不应 create_artifact / mark_done / update_state（不可改实现/调阶段）
    ],
    "Reviewer": [
        "send_message",
        "request_review",
        "respond_critique",
        "no_op",
    ],
    "Finalizer": [
        "send_message",
        "update_state",
        "respond_critique",
        "mark_done",  # 只有 Finalizer 真正宣布完成
        "no_op",
    ],
    "Researcher": [
        "send_message",
        "create_artifact",
        "handoff",
        "no_op",
    ],
}

# 允许的合法工具名（用于按 allowed_tools 过滤 ToolRegistry 注册的工具）
KNOWN_TOOL_NAMES: set[str] = {
    "search", "fetch_url", "read_file", "list_dir", "create_file",
    "edit_file", "execute", "memory_search", "memory_write",
}


def get_effective_allowed_actions(agent: AgentSpec) -> list[str]:
    """得到该 Agent 真正生效的 action 白名单。

    优先级：
    1. AgentSpec.allowed_actions 显式定义
    2. DEFAULT_ROLE_ALLOWED_ACTIONS[agent.role] 兜底
    3. 全开（[] 时视为不限制，向后兼容）
    """
    if agent.allowed_actions:
        return list(agent.allowed_actions)
    role_defaults = DEFAULT_ROLE_ALLOWED_ACTIONS.get(agent.role)
    if role_defaults:
        return list(role_defaults)
    # 角色未在默认表里且未显式声明：返回空表示"不限制"
    return []


def filter_actions_by_permission(
    agent: AgentSpec, actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 Agent 角色的 action 白名单过滤 LLM 产出的 actions。

    被拒绝的 action 替换为带拒绝信息的 no_op，便于可观测：只丢弃是不行的，
    会让 Agent 看起来"什么都没做"。我们要明确告诉下游：做了越权动作被拦截了。
    """
    allowed = get_effective_allowed_actions(agent)
    if not allowed:
        # 该角色无白名单约束 = 全开
        return actions

    allowed_set = set(allowed)
    result: list[dict[str, Any]] = []
    for action in actions:
        action_type = action.get("type", "no_op")
        if action_type in allowed_set:
            result.append(action)
            continue
        # 越权：替换为拒绝型 no_op
        logger.warning(
            f"[ActionGuard] agent={agent.name} role={agent.role} 越权 action={action_type} "
            f"被拒绝（允许列表：{sorted(allowed_set)}）"
        )
        result.append({
            "type": "no_op",
            "content": (
                f"[ActionGuard] Agent {agent.name}({agent.role}) 试图执行被禁用的动作 "
                f"'{action_type}'，已被运行时护栏拦截。允许的动作类型：{sorted(allowed_set)}"
            ),
            "rejected_action_type": action_type,
            "rejected_action": action,
        })
    return result


def filter_tools_by_permission(
    agent: AgentSpec, tools: list[Any]
) -> list[Any]:
    """按 AgentSpec.allowed_tools 过滤工具白名单。

    多 Agent 系统通过 action 协议通信，本函数主要给"附带工具"的运行模式使用
    （如果未来 AgentRuntimeAdapter 走 create_deep_agent + 真实工具集）。
    """
    if not agent.allowed_tools:
        # 未声明 = 全开（向后兼容旧 agent 定义）
        return tools
    allowed_tool_names = set(agent.allowed_tools)
    filtered: list[Any] = []
    for t in tools:
        tool_name = getattr(t, "name", str(t))
        if tool_name in allowed_tool_names:
            filtered.append(t)
        else:
            logger.debug(
                f"[ActionGuard] agent={agent.name} 工具 {tool_name} 未在 allowed_tools="
                f"{sorted(allowed_tool_names)} 中，已过滤掉"
            )
    return filtered


def is_action_allowed(agent: AgentSpec, action_type: str) -> bool:
    """判断单个 action 类型对该 Agent 是否允许。"""
    allowed = get_effective_allowed_actions(agent)
    if not allowed:
        return True
    return action_type in set(allowed)
