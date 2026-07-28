"""Regression coverage for the V1 run-event SSE transport."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from contextlib import suppress

import pytest
from fastapi.responses import StreamingResponse


router_module = importlib.import_module("app.api.v1.router")


class _History:
    def __init__(self, batches: list[list[dict]] | None = None) -> None:
        self.batches = list(batches or [])
        self.calls: list[tuple[str, int, int]] = []

    def list_event_envelopes(
        self, run_id: str, after_sequence: int, limit: int
    ) -> list[dict]:
        self.calls.append((run_id, after_sequence, limit))
        return self.batches.pop(0) if self.batches else []


def _configure_stream(monkeypatch: pytest.MonkeyPatch, history: _History) -> None:
    monkeypatch.setattr(router_module, "_require_run", lambda _run_id: {})
    monkeypatch.setattr(
        router_module, "get_agent_run_history", lambda: history
    )


@pytest.mark.asyncio
async def test_stream_is_native_async_and_preserves_resume_cursor(monkeypatch):
    history = _History(
        [
            [
                {
                    "sequence": 8,
                    "event_type": "assistant_token",
                    "payload": {"delta": "你"},
                },
                {
                    "sequence": 9,
                    "event_type": "assistant_token",
                    "payload": {"delta": "好"},
                },
            ],
            [
                {
                    "sequence": 10,
                    "event_type": "assistant_message",
                    "payload": {"content": "你好"},
                }
            ],
        ]
    )
    _configure_stream(monkeypatch, history)

    response = router_module.stream_events("run-1", after_sequence=7)

    assert isinstance(response, StreamingResponse)
    assert inspect.isasyncgen(response.body_iterator)
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    iterator = response.body_iterator
    chunks = [
        await anext(iterator),
        await anext(iterator),
        await anext(iterator),
    ]
    await iterator.aclose()

    assert [chunk.splitlines()[0] for chunk in chunks] == [
        "id: 8",
        "id: 9",
        "id: 10",
    ]
    payload = json.loads(chunks[-1].splitlines()[1].removeprefix("data: "))
    assert payload["payload"]["content"] == "你好"
    assert history.calls == [
        ("run-1", 7, 200),
        ("run-1", 9, 200),
    ]


@pytest.mark.asyncio
async def test_stream_idle_poll_yields_control_to_event_loop(monkeypatch):
    history = _History()
    _configure_stream(monkeypatch, history)
    sleep_started = asyncio.Event()
    sleep_blocker = asyncio.Event()

    async def blocked_sleep(delay: float) -> None:
        assert delay == 0.2
        sleep_started.set()
        await sleep_blocker.wait()

    monkeypatch.setattr(router_module, "_stream_sleep", blocked_sleep)
    response = router_module.stream_events("run-idle", after_sequence=0)
    iterator = response.body_iterator

    assert await anext(iterator) == ": keepalive\n\n"

    pending_chunk = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(sleep_started.wait(), timeout=0.2)
    event_loop_tick = asyncio.create_task(asyncio.sleep(0))
    await asyncio.wait_for(event_loop_tick, timeout=0.2)
    assert not pending_chunk.done()

    pending_chunk.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_chunk
    with suppress(RuntimeError):
        await iterator.aclose()


@pytest.mark.asyncio
async def test_stream_keeps_existing_idle_timeout_behavior(monkeypatch):
    history = _History()
    _configure_stream(monkeypatch, history)

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()

    async def advance_past_idle_deadline(delay: float) -> None:
        assert delay == 0.2
        clock.now += 301

    monkeypatch.setattr(router_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        router_module, "_stream_sleep", advance_past_idle_deadline
    )
    response = router_module.stream_events("run-timeout", after_sequence=0)
    iterator = response.body_iterator

    assert await anext(iterator) == ": keepalive\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)

