"""Pydantic 数据结构：任务、事件、产物、响应格式等。"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    user_input: str = Field(..., description="用户输入的任务内容")
    thread_id: str = Field(default_factory=lambda: "default", description="会话线程ID")


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    final_answer: str | None = None
    error_message: str | None = None


class ArtifactInfo(BaseModel):
    path: str = Field(..., description="产物在 workspace 的相对路径")
    name: str = Field(..., description="产物文件名")
    size_bytes: int = Field(default=0, description="文件大小")


class TaskEvent(BaseModel):
    event_type: str = Field(..., description="事件类型")
    data: dict[str, Any] = Field(default_factory=dict, description="事件数据")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    user_input: str
    thread_id: str
    final_answer: str | None = None
    artifacts: list[ArtifactInfo] = Field(default_factory=list)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    events: list[TaskEvent] = Field(default_factory=list)


class MemorySearchRequest(BaseModel):
    query: str = Field(..., description="检索关键词")
    limit: int = Field(default=5, ge=1, le=20, description="返回条数")


class MemorySearchResponse(BaseModel):
    query: str
    results: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None


class SkillInfo(BaseModel):
    name: str
    description: str
    path: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class SkillMeta(BaseModel):
    id: str
    name: str
    path: str
    description: str | None = None
    created_by: str = "user"
    source: str = "local"
    state: str = "active"
    pinned: bool = False
    bundled: bool = False
    hub_installed: bool = False
    version: int = 1
    content_hash: str | None = None
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    archived_at: str | None = None


class TaskResult(BaseModel):
    """AI 最终输出的结构化格式。"""
    summary: str = Field(description="任务结果摘要，前端直接展示给用户")
    artifacts: list[str] = Field(default_factory=list, description="产物文件路径列表（相对于 /workspace）")
    next_steps: list[str] = Field(default_factory=list, description="建议后续步骤")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="结果置信度 0-1")
