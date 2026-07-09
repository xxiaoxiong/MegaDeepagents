"""多智能体消息模型：AgentMessage + MessageType + MessageVisibility。

参考 MetaGPT 的 Message（content / cause_by / sent_from / send_to），
但工程化为更完整的 Pydantic 模型，便于落库与路由。

注意：本模块不依赖任何运行时组件，只暴露数据结构。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """多 Agent 之间结构化消息的类型。

    参考 MetaGPT 中"由 Action 产生的消息"概念。
    一种 MessageType 通常对应一种 Agent 行为的产物。
    """

    USER_REQUEST = "user_request"
    PLAN = "plan"
    DELEGATION = "delegation"
    QUESTION = "question"
    ANSWER = "answer"
    OBSERVATION = "observation"
    TOOL_RESULT = "tool_result"
    PROPOSAL = "proposal"
    CRITIQUE = "critique"
    REVISION_PLAN = "revision_plan"
    REVISION_DONE = "revision_done"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    HANDOFF = "handoff"
    DECISION = "decision"
    STATE_UPDATE = "state_update"
    ARTIFACT_CREATED = "artifact_created"
    FINAL = "final"
    ERROR = "error"
    BLOCKING_ISSUE = "blocking_issue"
    TEST_REQUEST = "test_request"
    TEST_RESULT = "test_result"
    RESEARCH_REQUEST = "research_request"
    TASK_ASSIGNMENT = "task_assignment"  # LLM 常自发使用，与 delegation 等价
    PROGRESS = "progress"  # 进度汇报
    NO_OP = "no_op"


# 别名映射：LLM 自创的类型名规整为系统标准 MessageType
_MESSAGE_TYPE_ALIASES: dict[str, str] = {
    "task_assignment": "delegation",
    "todo": "delegation",
    "assign": "delegation",
    "report": "observation",
    "result": "observation",
    "results": "observation",
    "summary": "observation",
    "feedback": "critique",
    "comment": "critique",
    "review": "review_result",
    "approve": "decision",
    "approval": "decision",
    "reject": "decision",
    "rejection": "decision",
    "complete": "final",
    "done": "final",
    "finish": "final",
    "finished": "final",
    "skip": "no_op",
    "wait": "no_op",
}


def normalize_message_type(raw: str) -> str:
    """把 LLM 可能给出的 message_type 字符串归一化为标准枚举值。返回归一化后的字符串。"""
    if not raw:
        return "observation"
    key = raw.strip().lower()
    if key in _MESSAGE_TYPE_ALIASES:
        return _MESSAGE_TYPE_ALIASES[key]
    return key


class MessageVisibility(str, Enum):
    """消息可见性，决定 MessageBus 的投递策略。"""

    BROADCAST = "broadcast"  # 投递给订阅该 message_type 的所有 Agent
    DIRECT = "direct"  # 仅投递给 to_agent 指定的 Agent
    SYSTEM = "system"  # 系统级消息，可投递给所有 Agent 或特定系统组件


class AgentMessage(BaseModel):
    """多 Agent 之间的结构化消息。

    与原有 `TaskMessage`（扁平 role/content）的区别：
    - 显式 from_agent / to_agent / visibility
    - 显式 message_type / cause_by / reply_to
    - evidence / artifact_refs 用于评审链路与产物引用
    - requires_response 用于驱动 SpeakerSelector
    """

    id: str = Field(..., description="消息唯一 ID")
    task_id: str = Field(..., description="所属任务 ID（兼容现有 task 任务）")
    room_id: str = Field(..., description="所属 TeamRoom ID")
    from_agent: str = Field(..., description="发送方 Agent 名称；system 代表框架")
    to_agent: str | list[str] | None = Field(
        default=None,
        description="接收方 Agent 名（visibility=direct 必填；broadcast 时为订阅者）",
    )
    visibility: MessageVisibility = Field(
        default=MessageVisibility.BROADCAST,
        description="广播 / 直接 / 系统",
    )
    message_type: MessageType = Field(..., description="消息类型")
    content: str = Field(default="", description="消息正文")
    cause_by: str | None = Field(
        default=None,
        description="触发该消息的来源动作或原因（对应 MetaGPT cause_by）",
    )
    reply_to: str | None = Field(default=None, description="回复哪条消息的 ID")
    thread_id: str | None = Field(default=None, description="话题线程 ID")
    requires_response: bool = Field(
        default=False,
        description="是否需要响应（影响 SpeakerSelector 优先级）",
    )
    expected_response_type: str | None = Field(
        default=None,
        description="期望响应的消息类型，便于对方对齐",
    )
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        description="证据条目，例如文件路径 + 行号 / 命令输出 / 引用段落",
    )
    artifact_refs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="相关产物引用：{path, name, role}",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="自由扩展元数据，例如 round、selected_speaker、tool_name",
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def to_broadcast_dict(self) -> dict[str, Any]:
        """供前端 / 落库使用的扁平字典。"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "room_id": self.room_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "visibility": self.visibility.value,
            "message_type": self.message_type.value,
            "content": self.content,
            "cause_by": self.cause_by,
            "reply_to": self.reply_to,
            "thread_id": self.thread_id,
            "requires_response": self.requires_response,
            "expected_response_type": self.expected_response_type,
            "evidence": self.evidence,
            "artifact_refs": self.artifact_refs,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


def make_message_id() -> str:
    """生成一个不带时间戳依赖的短消息 ID（兼容 workflow 限制）。"""
    import uuid

    return "msg_" + uuid.uuid4().hex[:12]
