"""Chat route: 提交对话任务。"""

import traceback
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.api.limiter import limiter
from app.core.config import settings
from app.core.logging import logger
from app.task.models import TaskEvent, TaskStatus
from app.task.runner import TaskRunner, get_pending_runner, set_pending_runner, remove_pending_runner
from app.task.service import get_task_service

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "api-default"
    auto_approve: bool = True

    @field_validator("message")
    @classmethod
    def validate_message_length(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be empty")
        if len(v) > settings.max_message_length:
            raise ValueError(f"message exceeds maximum length of {settings.max_message_length} characters")
        return v


class ChatResponse(BaseModel):
    status: str
    message: str | None = None
    task_id: str | None = None
    thread_id: str | None = None
    artifacts: list[dict[str, Any]] | None = None
    action_requests: list[dict[str, Any]] | None = None


@router.post("/chat", response_model=ChatResponse)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat(req: ChatRequest, request: Request):
    task_service = get_task_service()
    runner = TaskRunner(task_service, thread_id=req.thread_id, auto_approve=req.auto_approve)
    # 先创建 task，确保 task_id 立即可用
    task = task_service.create_task(req.message, req.thread_id)
    runner.task_id = task.task_id
    task_service.update_status(task.task_id, TaskStatus.RUNNING)
    task_service.add_message(task.task_id, "user", req.message, {"user_input": req.message})
    task_service.add_event(task.task_id, TaskEvent(
        event_type="runner_started",
        data={"user_input": req.message},
    ))
    # 后台执行，不阻塞 HTTP 返回
    import threading

    def _safe_run():
        try:
            runner.run(req.message)
        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"Background thread execution failed for task={task.task_id}: {exc}\n{traceback.format_exc()}")
            try:
                task_service.add_event(task.task_id, TaskEvent(
                    event_type="thread_execution_error",
                    data={"error": error_msg},
                ))
                task_service.mark_failed(task.task_id, error_msg)
            except Exception:
                pass

    thread = threading.Thread(target=_safe_run, daemon=True)
    thread.start()
    return ChatResponse(
        status="running",
        task_id=task.task_id,
        thread_id=req.thread_id,
    )
