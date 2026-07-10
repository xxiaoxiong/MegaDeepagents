"""TeamRunner 实时事件总线：SSE / WebSocket 后端。

设计：
1. TeamRunner 在主循环每个阶段（speaker 选择 / agent run / message publish / termination）
   调用 EventEmitter.emit(event_type, payload)
2. 本进程内的 SSE 端点 subscribe 到该 EventEmitter，把事件流式写给前端
3. EventEmitter 是进程内 asyncio.Queue 池 + 同步 queue 双实现，无需 redis
4. 事件按 task_id / room_id 分组；订阅者只取自己关心 key 的事件

事件类型：
- round_started
- speaker_selected
- agent_thought
- actions_emitted
- message_published
- state_updated
- review_request
- review_result
- artifact_created
- task_terminated
- error
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from typing import Any

from app.core.logging import logger
from app.core.observability import emit_trace_event


class _Subscription:
    """单订阅者队列：thread-safe，支持同步消费与异步消费两种模式。"""

    def __init__(self, key: str, maxsize: int = 200) -> None:
        self.key = key
        self._sync_queue: list[dict[str, Any]] = []
        self._async_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def put(self, event: dict[str, Any]) -> None:
        with self._lock:
            if len(self._sync_queue) >= self._maxsize:
                # 丢弃最旧，避免 OOM
                self._sync_queue.pop(0)
            self._sync_queue.append(event)
        if self._async_queue is not None:
            try:
                self._async_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"[EventEmitter] async queue full for key={self.key}")

    def drain_sync(self) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._sync_queue)
            self._sync_queue.clear()
            return events

    def sync_iter(self, timeout: float = 1.0, max_wait: float = 30.0):
        """同步阻塞迭代器：每 timeout 秒检查一次，最长 max_wait 秒静默后退出。

        供 FastAPI 同步路径使用（FastAPI 把同步 def 端点放到线程池跑）。
        """
        import time
        start = time.time()
        while True:
            with self._lock:
                if self._sync_queue:
                    event = self._sync_queue.pop(0)
                    yield event
                    start = time.time()  # 重置静默计时
                    continue
            if time.time() - start > max_wait:
                return
            time.sleep(timeout)


class EventEmitter:
    """进程内事件总线：按 key 分发到多个订阅者。

    使用场景：
    - TeamRunner 每个阶段 emit 事件
    - SSE 端点 subscribe(key) 拿到 _Subscription 流式输出
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscription]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, key: str, maxsize: int = 200) -> _Subscription:
        sub = _Subscription(key=key, maxsize=maxsize)
        with self._lock:
            self._subs[key].append(sub)
        logger.info(f"[EventEmitter] subscribe key={key}, total_subs={len(self._subs[key])}")
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        with self._lock:
            if sub in self._subs.get(sub.key, []):
                self._subs[sub.key].remove(sub)
                logger.info(f"[EventEmitter] unsubscribe key={sub.key}, remaining={len(self._subs[sub.key])}")

    def emit(self, key: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """向 key 的所有订阅者广播事件。无订阅者时静默丢弃。

        同时旁路分发一份到 LangSmith trace（emit_trace_event）。
        这样 6 类 SSE 事件自动成为当前 TeamRun trace 下的 child span signal。
        """
        with self._lock:
            subs = list(self._subs.get(key, []))
        if not subs:
            return
        event = {
            "event": event_type,
            "key": key,
            "payload": payload or {},
        }
        for sub in subs:
            try:
                sub.put(event)
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[EventEmitter] put failed key={key}: {exc}")
        # 旁路：LangSmith trace 信号
        emit_trace_event(event_type, payload)


# 进程级单例
_global_emitter: EventEmitter | None = None


def get_event_emitter() -> EventEmitter:
    global _global_emitter
    if _global_emitter is None:
        _global_emitter = EventEmitter()
    return _global_emitter
