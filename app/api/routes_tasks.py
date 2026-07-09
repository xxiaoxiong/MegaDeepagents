"""Task routes: 创建、查询、审批任务、文件下载与预览。"""

import asyncio
import concurrent.futures
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import logger
from app.core.schemas import TaskResponse
from app.task.service import get_task_service

router = APIRouter()
task_service = get_task_service()


class TaskCreateRequest(BaseModel):
    user_input: str
    thread_id: str = "default"
    auto_approve: bool = True


@router.get("/tasks", response_model=list[TaskResponse])
def list_tasks(limit: int = 20):
    tasks = task_service.list_tasks(limit)
    return [
        TaskResponse(
            task_id=t.task_id,
            status=t.status,
            user_input=t.user_input,
            thread_id=t.thread_id,
            final_answer=t.final_answer,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in tasks
    ]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    task = task_service.store.get_task_full(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse(
        task_id=task.task_id,
        status=task.status,
        user_input=task.user_input,
        thread_id=task.thread_id,
        final_answer=task.final_answer,
        artifacts=task.artifacts,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("/tasks/{task_id}/events")
def get_task_events(task_id: str):
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    events = task_service.get_events(task_id)
    return [
        {
            "event_type": ev.event_type,
            "data": ev.data,
            "created_at": ev.created_at.isoformat(),
        }
        for ev in events
    ]


@router.get("/tasks/{task_id}/messages")
def get_task_messages(task_id: str):
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    messages = task_service.get_messages(task_id)
    return [
        {
            "role": m["role"],
            "content": m["content"],
            "extra": m["extra"],
            "created_at": m["created_at"],
        }
        for m in messages
    ]


_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def shutdown_executor() -> None:
    """优雅关闭线程池，等待正在执行的任务最多 10 秒。"""
    _executor.shutdown(wait=True, cancel_futures=False)


def _approve_sync(task_id: str) -> dict:
    from app.task.models import TaskStatus
    from app.task.runner import get_pending_runner

    task = task_service.get_task(task_id)
    if not task:
        return {"status": "error", "error": "task not found"}
    if task.status.value not in {"waiting_approval", "running"}:
        return {"status": "already_processed", "task_id": task_id, "task_status": task.status.value}

    runner = get_pending_runner(task_id)
    if not runner:
        return {"status": "error", "error": "no pending runner for task"}

    try:
        logger.info(f"[approve] task_id={task_id}, thread_id={task.thread_id}")
        # 二次检查状态，防止并发重复审批
        fresh_task = task_service.get_task(task_id)
        if fresh_task and fresh_task.status.value not in {"waiting_approval", "running"}:
            return {"status": "already_processed", "task_id": task_id, "task_status": fresh_task.status.value}
        # 使用 DeepAgents 原生 HITL：runner 内部已保存 agent/config，
        # approve() 自动构造 decisions 并 resume，直到完成或再次中断
        result = runner.approve()
        content = runner._normalize_content(result)
        logger.info(f"Task approved and resumed: {task_id}")
        return {"status": "approved_and_resumed", "task_id": task_id, "result": content}
    except Exception as exc:
        tb = __import__("traceback").format_exc()
        logger.error(f"Resume task failed: {task_id}, error: {exc}\n{tb}")
        try:
            task_service.update_status(task_id, task.status)
        except Exception:
            pass
        return {"status": "approved", "task_id": task_id, "error": str(exc)}


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str):
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from app.task.models import TaskStatus
    if task.status.value == "waiting_approval":
        task_service.update_status(task_id, TaskStatus.RUNNING)
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, _approve_sync, task_id)
    try:
        res = await asyncio.shield(future)
    except asyncio.CancelledError:
        return {"status": "approved", "task_id": task_id}
    if isinstance(res, dict) and res.get("status") == "approved_and_resumed":
        task_service.mark_completed(task_id, res.get("result", ""))
    return res


@router.post("/tasks/{task_id}/reject")
def reject_task(task_id: str):
    from app.task.models import TaskStatus
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task_service.update_status(task_id, TaskStatus.CANCELLED)
    return {"status": "rejected", "task_id": task_id}


def _normalize_artifact_ref(path: str) -> str:
    """统一产物引用格式，用于比对。"""
    p = path.lstrip("/")
    if p.startswith("workspace/"):
        p = p[len("workspace/"):]
    return p


def _resolve_artifact_path(relative_path: str) -> Path:
    """将产物相对路径解析为 workspace 下的真实文件路径。"""
    clean = _normalize_artifact_ref(relative_path)
    candidate = Path(settings.workspace_dir) / clean
    # 防止路径穿越
    resolved = candidate.resolve()
    workspace_root = Path(settings.workspace_dir).resolve()
    if workspace_root not in resolved.parents and resolved != workspace_root:
        raise HTTPException(status_code=400, detail="Invalid artifact path")
    return resolved


@router.get("/tasks/{task_id}/artifacts/{artifact_path:path}")
def download_artifact(task_id: str, artifact_path: str):
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    file_path = _resolve_artifact_path(artifact_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@router.get("/tasks/{task_id}/preview/{artifact_path:path}")
def preview_artifact(task_id: str, artifact_path: str):
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    file_path = _resolve_artifact_path(artifact_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        content = "[无法读取该文件（可能是二进制文件）]"
    return {"content": content, "name": file_path.name, "path": artifact_path}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    ok = task_service.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Delete failed")
    return {"status": "deleted", "task_id": task_id}
