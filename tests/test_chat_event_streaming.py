"""对话式 UI 事件流后端测试。

覆盖 Track A 的四项核心改动：
- A1 EventEmitter → DB 事件库桥接（emit 自动落库 + payload.run_id 优先于 key）
- A2 token 级流式回调（assistant_token / assistant_message）
- A3 工具调用事件（tool_call_started / tool_call_result，含 tool_call_id 配对与 duration）
- A4 用户消息回显（broadcast_message 记录 user_message 事件）

这些事件是前端 ChatGPT 式对话 UI 的数据源——SSE 端点 /runs/{run_id}/stream
只从 DB 事件库 (list_event_envelopes) 读取，因此“落库”是到达前端的必要条件。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.database.run_store import get_agent_run_history
from app.multiagent.event_emitter import EventType, get_event_emitter


def _events(run_id: str, event_type: str | None = None) -> list[dict]:
    envs = get_agent_run_history().list_event_envelopes(run_id, 0, 500)
    if event_type is None:
        return envs
    return [e for e in envs if e["event_type"] == event_type]


# ===== A1: EventEmitter → DB 桥接 =====


def test_bridge_persists_emit_to_history():
    """emit 一个白名单事件 → list_event_envelopes 能查到。"""
    emitter = get_event_emitter()
    emitter.emit(
        "run_bridge_1",
        EventType.SPEAKER_SELECTED,
        {"run_id": "run_bridge_1", "agent": "Planner", "round": 1},
    )
    evs = _events("run_bridge_1", "speaker_selected")
    assert len(evs) == 1
    assert evs[0]["payload"]["agent"] == "Planner"
    assert evs[0]["sequence"] >= 1


def test_bridge_uses_payload_run_id_over_key():
    """emit key 是 room_id，payload 带 run_id —— 应按 run_id 落库，而非 room_id。

    这是 DISCUSSION 模式下 TeamRunner/round_executor 的场景：emit key=room_id
    （供内存订阅者），但事件需按真实 run_id 落库以供 SSE /runs/{run_id}/stream 流出。
    """
    emitter = get_event_emitter()
    emitter.emit(
        "room_abc",
        EventType.MESSAGE_PUBLISHED,
        {"run_id": "run_real_1", "from_agent": "Planner", "content": "hi"},
    )
    assert len(_events("run_real_1", "message_published")) == 1
    assert _events("room_abc", "message_published") == []


def test_bridge_skips_non_whitelisted_events():
    """非白名单事件类型不落库（避免 trace-only 事件污染对话流）。"""
    emitter = get_event_emitter()
    emitter.emit("run_skip_1", "some_random_type", {"run_id": "run_skip_1"})
    assert _events("run_skip_1") == []


def test_bridge_persists_without_in_memory_subscriber():
    """无内存订阅者时，事件仍应落库（SSE 读 DB，不依赖内存订阅）。"""
    emitter = get_event_emitter()
    emitter.emit(
        "run_nosub_1",
        EventType.TASK_TERMINATED,
        {"run_id": "run_nosub_1", "status": "completed"},
    )
    assert len(_events("run_nosub_1", "task_terminated")) == 1


# ===== A3: 工具调用事件 =====


def test_tool_hook_emits_started_and_result_paired():
    from app.multiagent.executor import _tool_hook

    run_id = "run_tool_1"
    _tool_hook(
        "BeforeToolUse", run_id=run_id, agent_id="a1", task_id="t1",
        tool_name="read_file", arguments={"file_path": "x.py"},
    )
    _tool_hook(
        "AfterToolUse", run_id=run_id, agent_id="a1", task_id="t1",
        tool_name="read_file", arguments={"file_path": "x.py"},
        result={"returncode": 0, "stdout": "ok"},
    )
    started = _events(run_id, "tool_call_started")
    result = _events(run_id, "tool_call_result")
    assert len(started) == 1
    assert len(result) == 1
    assert started[0]["payload"]["tool_name"] == "read_file"
    assert started[0]["payload"]["arguments"]["file_path"] == "x.py"
    # tool_call_id 前后配对一致
    assert started[0]["payload"]["tool_call_id"] == result[0]["payload"]["tool_call_id"]
    assert result[0]["payload"]["status"] == "ok"
    assert result[0]["payload"]["duration_ms"] >= 0
    assert "ok" in result[0]["payload"]["result_preview"]


def test_tool_hook_result_marks_error_on_nonzero_returncode():
    from app.multiagent.executor import _tool_hook

    run_id = "run_tool_err"
    _tool_hook(
        "BeforeToolUse", run_id=run_id, agent_id="a1", task_id="t1",
        tool_name="execute", arguments={"argv": ["ls"]},
    )
    _tool_hook(
        "AfterToolUse", run_id=run_id, agent_id="a1", task_id="t1",
        tool_name="execute", arguments={"argv": ["ls"]},
        result={"returncode": 2, "stderr": "boom"},
    )
    result = _events(run_id, "tool_call_result")
    assert result[0]["payload"]["status"] == "error"


def test_tool_hook_without_run_id_is_noop():
    from app.multiagent.executor import _tool_hook

    _tool_hook(
        "BeforeToolUse", run_id="", agent_id="a1", task_id="t1",
        tool_name="read_file", arguments={},
    )
    # 无 run_id 不落库
    assert _events("") == []


def test_read_tool_early_error_still_emits_paired_terminal_event(tmp_path):
    """Every admitted tool call must leave the UI with a terminal state."""
    from app.multiagent.executor import _build_restricted_tools

    run_id = "run_tool_missing_file"
    tool = _build_restricted_tools(
        allowed_tools=["read_file"],
        deny_default=True,
        task_workspace=str(tmp_path),
        run_id=run_id,
        agent_id="a1",
        task_id="t1",
    )[0]

    result_text = tool.invoke({"file_path": "missing.txt"})

    started = _events(run_id, "tool_call_started")
    result = _events(run_id, "tool_call_result")
    assert "文件不存在" in result_text
    assert len(started) == 1
    assert len(result) == 1
    assert started[0]["payload"]["tool_call_id"] == result[0]["payload"]["tool_call_id"]
    assert result[0]["payload"]["status"] == "error"


# ===== A2: token 级流式回调 =====


def test_assistant_stream_callback_emits_tokens_and_message():
    from app.multiagent.executor import _AssistantStreamCallback

    cb = _AssistantStreamCallback(
        run_id="run_tok_1", agent_id="a1", agent_name="Coder", throttle_s=0.0,
    )
    cb.on_chat_model_start(serialized={}, messages=[])
    cb.on_llm_new_token("Hello")
    cb.on_llm_new_token(", ")
    cb.on_llm_new_token("world!")
    cb.on_llm_end(response=None)

    tokens = _events("run_tok_1", "assistant_token")
    messages = _events("run_tok_1", "assistant_message")
    assert len(messages) == 1
    assert messages[0]["payload"]["content"] == "Hello, world!"
    msg_id = messages[0]["payload"]["message_id"]
    assert msg_id
    # 所有 token chunk 的 message_id 与最终消息一致
    assert tokens, "应至少有一个 assistant_token 事件"
    assert all(t["payload"]["message_id"] == msg_id for t in tokens)
    # token delta 拼接 == 完整内容
    assert "".join(t["payload"]["delta"] for t in tokens) == "Hello, world!"
    assert messages[0]["payload"]["agent_name"] == "Coder"


def test_assistant_stream_callback_no_run_id_is_noop():
    from app.multiagent.executor import _AssistantStreamCallback

    cb = _AssistantStreamCallback(run_id="", agent_id="", agent_name="X", throttle_s=0.0)
    cb.on_chat_model_start(serialized={}, messages=[])
    cb.on_llm_new_token("x")
    cb.on_llm_end(response=None)
    assert _events("") == []


def test_assistant_stream_callback_throttle_batches_tokens():
    """throttle_s>0 时，多个 token 应被合并成更少的 chunk 事件。"""
    from app.multiagent.executor import _AssistantStreamCallback

    cb = _AssistantStreamCallback(
        run_id="run_tok_2", agent_id="a1", agent_name="Coder", throttle_s=60.0,
    )
    cb.on_chat_model_start(serialized={}, messages=[])
    for ch in "abcdef":
        cb.on_llm_new_token(ch)
    cb.on_llm_end(response=None)

    tokens = _events("run_tok_2", "assistant_token")
    # 6 个 token 但 throttle=60s，应合并成 1 个 chunk（on_llm_end 时 flush）
    assert len(tokens) == 1
    assert tokens[0]["payload"]["delta"] == "abcdef"


def test_assistant_stream_callback_uses_authoritative_final_content():
    """The final provider message repairs any missing streamed delta."""
    from app.multiagent.executor import _AssistantStreamCallback

    cb = _AssistantStreamCallback(
        run_id="run_tok_final",
        agent_id="a1",
        agent_name="Coder",
        throttle_s=60.0,
    )
    cb.on_chat_model_start(serialized={}, messages=[])
    cb.on_llm_new_token("部分")
    response = SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(content="部分但最终完整的回答")
                )
            ]
        ]
    )

    cb.on_llm_end(response=response)

    message = _events("run_tok_final", "assistant_message")[0]["payload"]
    assert message["content"] == "部分但最终完整的回答"
    assert message["finish_reason"] == "completed"


def test_assistant_stream_callback_error_finalizes_partial_message():
    """A model error must stop the live cursor while preserving partial text."""
    from app.multiagent.executor import _AssistantStreamCallback

    cb = _AssistantStreamCallback(
        run_id="run_tok_error",
        agent_id="a1",
        agent_name="Coder",
        throttle_s=60.0,
    )
    cb.on_chat_model_start(serialized={}, messages=[])
    cb.on_llm_new_token("已经生成的部分")

    cb.on_llm_error(RuntimeError("gateway disconnected"))

    message = _events("run_tok_error", "assistant_message")[0]["payload"]
    assert message["content"] == "已经生成的部分"
    assert message["finish_reason"] == "error"
    assert message["error"] == "gateway disconnected"


# ===== A4: 用户消息回显 =====


@pytest.mark.asyncio
async def test_broadcast_message_records_user_message():
    from app.application.runs.service import RunApplicationService

    history = get_agent_run_history()
    run_id = "run_user_1"
    history.save_team_run(
        run_id=run_id, goal="g", team_id="t", mode="team",
        workspace_root="/tmp/ws", status="running", max_rounds=10,
        review_required=False, metadata={},
    )
    svc = RunApplicationService()
    delivered = await svc.broadcast_message(run_id, "请帮我重构前端")
    # 无 agent 时 delivered=0，但 user_message 事件仍应已记录（在投递循环之前）
    assert delivered == 0
    evs = _events(run_id, "user_message")
    assert len(evs) == 1
    assert evs[0]["payload"]["content"] == "请帮我重构前端"
    assert evs[0]["payload"]["role"] == "user"
    assert evs[0]["payload"]["source"] == "human"


@pytest.mark.asyncio
async def test_broadcast_message_unknown_run_records_nothing():
    from app.application.runs.service import RunApplicationService

    svc = RunApplicationService()
    delivered = await svc.broadcast_message("run_nonexistent", "hello")
    assert delivered == 0
    assert _events("run_nonexistent") == []
