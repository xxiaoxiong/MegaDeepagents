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


# ===== 事件类型常量（前后端共用命名）=====
class EventType:
    # TeamRunner / round_executor 已有
    TASK_STARTED = "task_started"
    TERMINATION = "termination"
    TASK_TERMINATED = "task_terminated"
    ROUND_STARTED = "round_started"
    SPEAKER_SELECTED = "speaker_selected"
    AGENT_THOUGHT = "agent_thought"
    ACTIONS_EMITTED = "actions_emitted"
    MESSAGE_PUBLISHED = "message_published"
    STATE_UPDATED = "state_updated"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    ARTIFACT_CREATED = "artifact_created"
    ERROR = "error"
    # 新增：对话式 UI 所需
    USER_MESSAGE = "user_message"
    ASSISTANT_TOKEN = "assistant_token"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_RESULT = "tool_call_result"


# 落库白名单：只有这些事件类型会同步写入 DB 事件库（供 SSE 流出 + 回放）。
# 高频事件（如 ASSISTANT_TOKEN）应在发射端节流后再 emit，避免 DB 压力。
_PERSIST_EVENT_TYPES: frozenset[str] = frozenset({
    EventType.TASK_STARTED,
    EventType.TERMINATION,
    EventType.TASK_TERMINATED,
    EventType.ROUND_STARTED,
    EventType.SPEAKER_SELECTED,
    EventType.AGENT_THOUGHT,
    EventType.ACTIONS_EMITTED,
    EventType.MESSAGE_PUBLISHED,
    EventType.STATE_UPDATED,
    EventType.REVIEW_REQUEST,
    EventType.REVIEW_RESULT,
    EventType.ARTIFACT_CREATED,
    EventType.ERROR,
    EventType.USER_MESSAGE,
    EventType.ASSISTANT_TOKEN,
    EventType.ASSISTANT_MESSAGE,
    EventType.TOOL_CALL_STARTED,
    EventType.TOOL_CALL_RESULT,
})


def _persist_event_to_history(
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """把事件同步落库到 AgentRunHistory，使其经 SSE 端点流出且可回放。

    SSE 端点 ``GET /api/v1/runs/{run_id}/stream`` 只从 DB 事件库
    (``list_event_envelopes``) 读取，不订阅内存 EventEmitter。因此 round_executor
    的富事件必须在此落库才能到达前端。任何异常都吞掉并 warn——事件落库失败
    不应阻断运行时主流程。
    """
    if not run_id:
        return
    if event_type not in _PERSIST_EVENT_TYPES:
        return
    # 优先用 payload 里的 run_id 落库：TeamRunner/round_executor 的 emit key 是
    # room_id（独立 UUID，供内存订阅者 routes_team.stream_team_task_events 使用），
    # 而 SSE 端点 /runs/{run_id}/stream 按真实 run_id 查询。因此发射端需在 payload
    # 里带 run_id（DISCUSSION 模式下 task_id == run_id），桥接据此落库到正确 run_id。
    effective_run_id = payload.get("run_id") or run_id
    try:
        # 延迟导入避免循环依赖
        from app.infrastructure.database.run_store import (
            get_agent_run_history,
            make_run_event_id,
        )
        agent_id = payload.get("agent_id") or payload.get("agent") or None
        task_id = payload.get("task_id") or None
        get_agent_run_history().record_event(
            event_id=make_run_event_id(),
            run_id=str(effective_run_id),
            event_type=event_type,
            agent_id=str(agent_id) if agent_id is not None else None,
            task_id=str(task_id) if task_id is not None else None,
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning(
            f"[EventEmitter] persist failed type={event_type} run={run_id}: {exc}"
        )


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

        三路分发：
        1. 同步落库到 AgentRunHistory（``_persist_event_to_history``）——供 SSE 端点
           ``/runs/{run_id}/stream`` 流出与历史回放（前端对话式 UI 的数据源）。
           无论是否有内存订阅者都需落库，因为 SSE 端点只读 DB。
        2. 进程内订阅者队列（``sub.put``）——供同进程 SSE / 其他监听者。
        3. LangSmith trace 旁路（``emit_trace_event``）——成为 TeamRun trace 下的 child span。
        """
        payload = payload or {}
        # 1. 落库（供 SSE 流出 + 回放）
        _persist_event_to_history(key, event_type, payload)
        # 2. 内存订阅者
        with self._lock:
            subs = list(self._subs.get(key, []))
        if subs:
            event = {
                "event": event_type,
                "key": key,
                "payload": payload,
            }
            for sub in subs:
                try:
                    sub.put(event)
                except Exception as exc:  # pragma: no cover
                    logger.warning(f"[EventEmitter] put failed key={key}: {exc}")
        # 3. LangSmith trace 旁路
        emit_trace_event(event_type, payload)


# 进程级单例
_global_emitter: EventEmitter | None = None


def get_event_emitter() -> EventEmitter:
    global _global_emitter
    if _global_emitter is None:
        _global_emitter = EventEmitter()
    return _global_emitter
