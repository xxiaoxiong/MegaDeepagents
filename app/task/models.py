"""Task 数据模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.schemas import ArtifactInfo, TaskEvent, TaskStatus


class TaskMessage(BaseModel):
    role: str = Field(..., description="user | assistant | system | tool")
    content: str = Field(default="", description="消息内容")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展信息，如 tool name、status 等")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Task(BaseModel):
    task_id: str
    user_input: str
    status: TaskStatus = TaskStatus.CREATED
    thread_id: str = "default"
    final_answer: str | None = None
    artifacts: list[ArtifactInfo] = Field(default_factory=list)
    error_message: str | None = None
    events: list[TaskEvent] = Field(default_factory=list)
    messages: list[TaskMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
