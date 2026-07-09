"""AgentRuntimeAdapter：复用现有 DeepAgents 能力作为每个 TeamAgent 的执行内核。

设计原则：
1. AgentRuntimeAdapter.run(agent_spec, inbox_messages, shared_state, ...) → list[AgentMessage]
2. 内部调用现有 build_model() 为每个 Agent 创建 LLM 实例
3. 加入 Agent 级工具白名单拦截：按 AgentSpec.allowed_tools 过滤注册的工具
4. 加入 Agent 级 action 类型白名单拦截：按 AgentSpec.allowed_actions 过滤 action 类型
5. 要求 Agent 输出结构化 JSON（actions），不可自由输出无法解析的大段文本
6. 解析失败时走 fallback（包装为 observation 消息）

注意：实际运行时，由于每个 Agent 都是同一个 model + tools + backend 的 DeepAgent，
所以运行时适配器返回的是"预构造的响应"，而非真正的独立 LLM 调用。
在初期实现中，AgentRuntimeAdapter 响应由 prompt 驱动，不使用独立的第三方模型调用。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import logger
from app.multiagent.action_guard import filter_actions_by_permission
from app.multiagent.agent_spec import AgentSpec
from app.multiagent.messages import (
    AgentMessage,
    MessageVisibility,
    MessageType,
    make_message_id,
    normalize_message_type,
)
from app.multiagent.state import SharedTeamState


# 描述可用的 action 类型
ACTION_SCHEMA = {
    "title": "AgentActions",
    "description": "你本轮必须产出的动作列表。",
    "type": "object",
    "properties": {
        "thought_summary": {
            "type": "string",
            "description": "简短说明本轮判断（不暴露冗长推理）。",
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "send_message",
                            "update_state",
                            "create_artifact",
                            "request_review",
                            "respond_critique",
                            "mark_done",
                            "handoff",
                            "no_op",
                        ],
                        "description": "动作类型",
                    },
                    "to_agent": {"type": "string", "description": "接收方 Agent 名"},
                    "message_type": {"type": "string", "description": "消息类型"},
                    "content": {"type": "string", "description": "消息正文"},
                    "requires_response": {"type": "boolean"},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                    "artifact_refs": {"type": "array", "items": {"type": "object"}},
                    "patch": {"type": "object", "description": "state patch"},
                    "artifact_path": {"type": "string", "description": "产物路径"},
                    "artifact_role": {"type": "string", "description": "产物角色"},
                    "issuelist": {"type": "array", "items": {"type": "object"}},
                    "issue_id": {"type": "string"},
                    "issue_status": {"type": "string"},
                    "phase": {"type": "string"},
                    "plan": {"type": "string"},
                    "review_result": {
                        "type": "object",
                        "properties": {
                            "passed": {"type": "boolean"},
                            "issues": {"type": "array", "items": {"type": "object"}},
                            "required_fix_owner": {"type": "string"},
                        },
                    },
                    "final_output": {"type": "string"},
                },
                "required": ["type"],
            },
        },
    },
    "required": ["thought_summary", "actions"],
}


class AgentRuntimeAdapter:
    """Agent 运行时适配器：封装 agent 执行，产生结构化 actions。

    当前版本为"直执行模式"：Agent 不真正调用 LLM，而是通过 system_prompt 模板
    在 prompt 环节构造"预期响应"。未来可扩展为独立 LLM 调用。
    """

    def __init__(self, task_id: str, room_id: str):
        self.task_id = task_id
        self.room_id = room_id

    def build_system_prompt(
        self,
        agent: AgentSpec,
        shared_state: SharedTeamState,
        inbox_context: str,
        team_agents: list[AgentSpec] | None = None,
        recent_actions: dict[str, Any] | None = None,
    ) -> str:
        """构造该 Agent 本轮使用的 system prompt。"""
        from app.multiagent.action_guard import get_effective_allowed_actions

        parts: list[str] = []

        # 角色定义
        parts.append(f"# 你的身份\n你是 {agent.role}（{agent.name}）。")
        parts.append(f"目标：{agent.goal}")
        if agent.system_prompt:
            parts.append(f"## 角色额外说明\n{agent.system_prompt}")

        # 角色边界（工具白名单）
        parts.append("## 你可以做（工具白名单）")
        parts.append("\n".join(f"- {a}" for a in agent.allowed_tools or ["发送消息"]))

        # 角色边界（action 白名单，运行时强制）
        allowed = get_effective_allowed_actions(agent)
        if allowed:
            parts.append(
                "## 你能产出的 action 类型（系统强制白名单，越权会被拦截并改为 no_op）\n"
                + ", ".join(sorted(allowed))
            )

        # 团队成员名单（关键：阻止 LLM 编造不存在的 agent 名）
        if team_agents:
            other_agents = [a for a in team_agents if a.name != agent.name]
            if other_agents:
                lines = []
                for a in other_agents:
                    lines.append(f"- {a.name}（{a.role}）：{a.goal}")
                parts.append("## 团队成员（你只能向以下名字发消息）\n" + "\n".join(lines))

        # 团队状态
        parts.append("## 当前团队状态")
        parts.append(shared_state.to_prompt_context())

        # Inbox
        parts.append("## 你的收件箱")
        parts.append(inbox_context or "(无新消息)")

        # 输出格式
        parts.append(
            "## 输出要求\n"
            "你必须输出一个 JSON 对象，包含以下顶级字段：\n"
            '- "thought_summary": 简短说明本轮判断的文本。\n'
            '- "actions": 一个数组，每个元素是一个动作对象。每轮至少产生一个有效动作。\n\n'
            "支持的 action type 与结构：\n"
            '- send_message: {"type":"send_message","to_agent":"团队中存在的agent名","message_type":"plan|delegation|critique|review_request|review_result|test_request|test_result|handoff|decision|observation|final","content":"...","requires_response":true}\n'
            '- update_state: {"type":"update_state","patch":{"phase":"planning|executing|reviewing|repairing|finalizing","plan":"..."}}\n'
            '- create_artifact: {"type":"create_artifact","artifact_path":"...","artifact_role":"...","version":1}\n'
            '- request_review: {"type":"request_review","to_agent":"ReviewerAgent","content":"请评审..."}\n'
            '- respond_critique: {"type":"respond_critique","to_agent":"ReviewerAgent","content":"已修复..."}\n'
            '- mark_done: {"type":"mark_done","content":"任务完成说明"}  （仅 Finalizer 允许使用）\n'
            '- handoff: {"type":"handoff","to_agent":"团队中存在的agent名","content":"请你继续..."}\n'
            '- no_op: {"type":"no_op","content":"本轮无需行动（必须附理由）"}\n\n'
            "**关键规则**：\n"
            "1. to_agent 必须从上面团队成员名单中选取，禁止编造不存在的 agent 名。\n"
            "2. message_type 必须从上面列举的合法值中选取。\n"
            "3. 禁止输出自由文本、对话、markdown 代码框等非结构化内容。\n"
            "4. 你的输出必须仅包含 JSON 对象。\n"
            "5. 你只能产出上面白名单中允许的 action 类型；越权动作会被运行时拦截并改为 no_op。"
        )

        return "\n\n".join(parts)

    def run(
        self,
        agent: AgentSpec,
        inbox_messages: list[AgentMessage],
        shared_state: SharedTeamState,
        workspace_path: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """执行 Agent 本轮任务，返回 actions 列表（符合 ACTION_SCHEMA 中的 action 定义）。

        实际流程：
        1. 构造 system prompt + user prompt（团队状态 + 收件箱）
        2. 调用 build_model() 得到 LLM 实例
        3. invoke 得到文本，解析为 JSON
        4. 若解析失败，回退用一个 no_op 包装该文本作为容错
        """
        inbox_context = "\n".join(
            f"- from {m.from_agent} [{m.message_type.value}]: {m.content[:200]}"
            for m in inbox_messages
        ) if inbox_messages else "(无消息)"

        full_prompt = self.build_system_prompt(agent, shared_state, inbox_context)

        logger.debug(
            f"[AgentRuntimeAdapter] run: agent={agent.name}, "
            f"inbox={len(inbox_messages)}, "
            f"shared_state phase={shared_state.phase.value}"
        )

        # ---- 实际调用 LLM ----
        actions = self._call_llm(agent, full_prompt)
        if not actions:
            # 解析失败回退：用一个 no_op 包装原文本，避免卡住循环
            logger.warning(
                f"[AgentRuntimeAdapter] agent={agent.name} LLM 输出无法解析为 actions，回退 no_op"
            )
            return [{
                "type": "no_op",
                "content": f"Agent {agent.name} 输出无法解析为结构化动作（已回退）",
            }]
        # ---- 运行时 action 白名单强制隔离（防止角色越权）----
        # 例如阻挡 Reviewer create_artifact / 阻挡 Coder mark_done / 阻挡 Planner 直接收尾
        if agent.allowed_actions or agent.role:
            actions = filter_actions_by_permission(agent, actions)
        return actions

    def _call_llm(self, agent: AgentSpec, prompt: str) -> list[dict[str, Any]]:
        """真正调用 LLM 并解析为 actions 列表。失败返回空列表。

        重试策略：仅对可恢复异常重试（404 / 429 / 5xx / 网络），
        业务错误不重试。最多 3 次，退避间隔 0.6→1.5→3s。
        """
        from app.llm_factory import build_model

        max_retries = 3
        last_error: str | None = None

        for attempt in range(1, max_retries + 1):
            try:
                llm = build_model()
                # 使用 JSON mode 提高结构化输出成功率
                try:
                    json_llm = llm.bind(response_format={"type": "json_object"})
                except Exception:
                    json_llm = llm
                response = json_llm.invoke([("user", prompt)])
                text = getattr(response, "content", str(response))
                if isinstance(text, list):
                    text = json.dumps(text, ensure_ascii=False)
                parsed = self.parse_json_response(text)
                if not parsed:
                    logger.warning(
                        f"[AgentRuntimeAdapter] agent={agent.name} 第{attempt}次无法解析 JSON。"
                        f"原始输出前200字：{text[:200]}"
                    )
                    if attempt < max_retries:
                        continue
                    return []
                actions = parsed.get("actions") or []
                if not isinstance(actions, list):
                    if attempt < max_retries:
                        continue
                    return []
                valid = [a for a in actions if isinstance(a, dict) and a.get("type")]
                return valid
            except Exception as exc:
                last_error = str(exc)
                err_str = str(exc)
                # 判断是否为可恢复异常
                is_retryable = any(
                    code in err_str
                    for code in ["403", "429", "500", "502", "503", "timeout", "Timeout", "Connection"]
                )
                # 404 比较特殊：可能是上游间歇问题（已验证）
                if "404" in err_str or is_retryable:
                    if attempt < max_retries:
                        import time
                        backoff = {1: 0.6, 2: 1.5, 3: 3.0}.get(attempt, 1.0)
                        logger.info(
                            f"[AgentRuntimeAdapter] agent={agent.name} "
                            f"第{attempt}次调用异常（{last_error}），{backoff}s 后第{attempt+1}次重试"
                        )
                        time.sleep(backoff)
                        continue
                else:
                    # 非重试性异常直接退出
                    logger.error(
                        f"[AgentRuntimeAdapter] agent={agent.name} "
                        f"不可恢复异常：{last_error}"
                    )
                    return []

        logger.error(
            f"[AgentRuntimeAdapter] agent={agent.name} "
            f"重试{max_retries}次全部失败，最终错误：{last_error}"
        )
        return []

    @staticmethod
    def parse_json_response(response_text: str) -> dict[str, Any] | None:
        """从 Agent 响应中解析 JSON。三级回退。"""
        text = response_text.strip()
        # 路径 1：直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 路径 2：提取 ```json ... ``` 块
        import re
        m = re.search(
            r"```(?:json)?\s*\n(.*?)\n```",
            text,
            re.DOTALL,
        )
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 路径 3：brace-balanced 截取首个 { 到末个 }
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            # 路径 4：ast.literal_eval 容错 Python-style 单引号 JSON
            try:
                import ast
                return ast.literal_eval(candidate)
            except Exception:
                pass
        return None

    @staticmethod
    def actions_to_messages(
        agent_name: str,
        task_id: str,
        room_id: str,
        actions: list[dict[str, Any]],
        round_number: int,
    ) -> list[AgentMessage]:
        """将 actions 转换为 AgentMessage 列表（供 bus.publish 使用）。"""
        messages: list[AgentMessage] = []
        for action in actions:
            action_type = action.get("type", "no_op")
            # 优先使用 LLM 显式声明的 message_type；缺失时才用 action→message_type 映射
            # 这样 send_message(message_type="plan") 才能正确路由到订阅了 PLAN 的 Coder 等 Agent
            msg_type_str = action.get("message_type") or _action_to_message_type(action_type)
            # 归一化 LLM 的梦话类型
            normalized = normalize_message_type(msg_type_str)
            try:
                msg_type_enum = MessageType(normalized)
            except ValueError:
                logger.warning(
                    f"[AgentRuntimeAdapter] unknown message_type={normalized!r} "
                    f"(orig={msg_type_str!r}) from action={action_type}, fallback to mapping"
                )
                msg_type_enum = MessageType(_action_to_message_type(action_type))
            # send_message 类型决定 visibility：有 to_agent 用 direct，否则 broadcast
            has_to = bool(action.get("to_agent"))
            if action_type == "no_op":
                visibility = MessageVisibility.BROADCAST
            elif has_to:
                visibility = MessageVisibility.DIRECT
            else:
                visibility = MessageVisibility.BROADCAST
            msg = AgentMessage(
                id=make_message_id(),
                task_id=task_id,
                room_id=room_id,
                from_agent=agent_name,
                to_agent=action.get("to_agent"),
                visibility=visibility,
                message_type=msg_type_enum,
                content=action.get("content", ""),
                requires_response=action.get("requires_response", False),
                evidence=action.get("evidence", []),
                artifact_refs=action.get("artifact_refs", []),
                cause_by=action_type,
                reply_to=action.get("reply_to"),
                metadata={"round": round_number, "action_type": action_type},
            )
            messages.append(msg)
        return messages


def _action_to_message_type(action_type: str) -> str:
    mapping = {
        "send_message": "observation",
        "update_state": "state_update",
        "create_artifact": "artifact_created",
        "request_review": "review_request",
        "respond_critique": "observation",
        "mark_done": "final",
        "handoff": "handoff",
        "no_op": "no_op",
    }
    return mapping.get(action_type, "observation")
