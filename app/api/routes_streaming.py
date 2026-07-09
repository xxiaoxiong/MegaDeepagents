"""Streaming routes: 提供任务事件的 SSE 端点。"""

import json
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.logging import logger
from app.task.runner import get_stream_queue, remove_stream_queue

router = APIRouter()


def _format_sse(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


@router.get("/tasks/{task_id}/stream")
def stream_task_events(task_id: str):
    """SSE 端点：推送任务实时事件流。"""
    if not settings.enable_streaming:
        # 当 streaming 关闭时，返回一个 SSE 说明事件，客户端可据此降级到轮询
        return StreamingResponse(
            iter([_format_sse("info", {"message": "streaming_disabled", "fallback": "polling"})]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    queue = get_stream_queue(task_id)
    if queue is None:
        return StreamingResponse(
            iter([_format_sse("error", {"message": "task_not_found_or_not_running"})]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    heartbeat_interval = settings.stream_heartbeat_interval

    def event_generator():
        sent_completed = False
        try:
            while True:
                # 使用超时等待事件，避免无限阻塞
                try:
                    item = queue.get_nowait()
                except Exception:
                    item = None

                if item is None:
                    # 心跳包，保持连接活跃
                    yield _format_sse("heartbeat", {"time": time.time()})
                    time.sleep(heartbeat_interval)
                    continue

                event = item.get("event", "unknown")
                data = item.get("data", {})

                if event == "task_completed":
                    sent_completed = True

                yield _format_sse(event, data)

                if sent_completed:
                    # 发送完成事件后关闭流
                    break
        except GeneratorExit:
            logger.debug(f"SSE client disconnected for task={task_id}")
        finally:
            # 客户端断开时清理可能残留的无消费者队列
            if not sent_completed:
                remove_stream_queue(task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
