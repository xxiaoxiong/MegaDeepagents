"""AgentInbox：每个 Agent 的私有收件箱。

类比 MetaGPT 的 Role.msg_buffer。Agent 每轮只读取自己 inbox 中的相关消息，
不默认读取完整聊天记录，避免上下文污染。

Inbox 通过 store 后端持久化，但可由 Inbox 类做在线层抽象：
- list_unread(agent_name)：列出未读消息
- mark_read(message_id)：标记已读
- get_relevant_context(agent_name, max_items)：构造供 Agent 输入的 inbox 上下文
- summarize_old_messages(agent_name)：旧消息压缩为摘要
"""

from __future__ import annotations

from typing import Any

from app.multiagent.messages import AgentMessage, MessageType


class AgentInbox:
    """Agent 收件箱。包装 store 的 inbox 操作，并加上下文格式化能力。"""

    def __init__(self, store: Any, room_id: str, task_id: str):
        self.store = store
        self.room_id = room_id
        self.task_id = task_id

    def list_unread(self, agent_name: str) -> list[AgentMessage]:
        if not self.store:
            return []
        return self.store.get_agent_unread_inbox(self.room_id, agent_name)

    def list_all(self, agent_name: str) -> list[AgentMessage]:
        if not self.store:
            return []
        return self.store.get_agent_full_inbox(self.room_id, agent_name)

    def mark_read(self, message_id: str, agent_name: str) -> None:
        if not self.store:
            return
        self.store.ack_message(message_id, agent_name)

    def mark_all_read(self, agent_name: str) -> int:
        count = 0
        for m in self.list_unread(agent_name):
            self.mark_read(m.id, agent_name)
            count += 1
        return count

    def get_relevant_context(self, agent_name: str, max_items: int = 8) -> str:
        """生成 inbox 摘要文本，供 Agent system prompt 使用。"""
        unread = self.list_unread(agent_name)
        if not unread:
            return "(暂无未读消息)"
        # 优先级排序：requires_response > direct > 重要类型
        priority_types = {
            MessageType.USER_REQUEST,
            MessageType.CRITIQUE,
            MessageType.REVIEW_REQUEST,
            MessageType.TEST_REQUEST,
            MessageType.DELEGATION,
            MessageType.REVISION_PLAN,
            MessageType.QUESTION,
        }

        def _sort_key(m: AgentMessage):
            score = 0
            if m.requires_response:
                score += 100
            if m.visibility.value == "direct":
                score += 50
            if m.message_type in priority_types:
                score += 20
            return -score

        sorted_msgs = sorted(unread, key=_sort_key)[:max_items]
        lines: list[str] = []
        for m in sorted_msgs:
            meta_parts = [f"type={m.message_type.value}"]
            if m.requires_response:
                meta_parts.append("NEEDS_RESPONSE")
            vstr = m.visibility.value
            tgt = m.to_agent if isinstance(m.to_agent, str) else (
                ",".join(m.to_agent) if isinstance(m.to_agent, list) else "all"
            )
            meta_parts.append(f"from={m.from_agent} to={tgt} vis={vstr}")
            if m.expected_response_type:
                meta_parts.append(f"expect={m.expected_response_type}")
            line = f"### message id={m.id} [{', '.join(meta_parts)}]\n{m.content}"
            if m.evidence:
                ev = "; ".join(
                    f"{e.get('kind', '?')}:{e.get('detail', '')}" for e in m.evidence[:3]
                )
                line += f"\n(证据: {ev})"
            if m.artifact_refs:
                ar = "; ".join(a.get("path", "") for a in m.artifact_refs[:3])
                line += f"\n(相关产物: {ar})"
            lines.append(line)
        return "\n\n".join(lines)

    def summarize_old_messages(self, agent_name: str, keep_recent: int = 4) -> str:
        """将较老的已读消息压缩为一段摘要。

        简化实现：保留最近 keep_recent 条未读，其余批次做粗摘要。
        真实场景下可用 LLM 摘要；本文本能力足够运行。
        """
        all_msgs = self.list_all(agent_name)
        if len(all_msgs) <= keep_recent:
            return ""
        old_msgs = all_msgs[:-keep_recent]
        old = all_msgs[-keep_recent:]
        summary_lines: list[str] = []
        type_counts: dict[str, int] = {}
        for m in old_msgs:
            type_counts[m.message_type.value] = type_counts.get(m.message_type.value, 0) + 1
        summary_lines.append(
            "# 已读历史摘要\n共 {} 条历史消息，按类型：{}".format(
                len(old_msgs), ", ".join(f"{k}×{v}" for k, v in type_counts.items())
            )
        )
        summary_lines.append("# 最近 {} 条".format(len(old)))
        for m in old:
            summary_lines.append(f"- [{m.message_type.value}] {m.from_agent}: {m.content[:120]}")
        return "\n".join(summary_lines)
