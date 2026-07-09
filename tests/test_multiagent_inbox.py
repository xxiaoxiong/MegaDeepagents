"""MultiAgent AgentInbox 测试，使用真实 MultiAgentStore SQLite。"""

import os
import tempfile
import uuid

from app.multiagent.agent_spec import AgentSpec, TeamSpec, TeamRunConfig
from app.multiagent.bus import MessageBus
from app.multiagent.default_teams import SOFTWARE_DEV_TEAM
from app.multiagent.inbox import AgentInbox
from app.multiagent.room import TeamRoom
from app.multiagent.messages import MessageType, AgentMessage, MessageVisibility, make_message_id


def _fresh_store(tmp_path):
    """返回一个使用临时库的新 store。"""
    from app.multiagent import store as ma_store
    ma_store.close_connection()
    # 临时切换 db 路径
    import app.core.config as cfg
    cfg.settings.sqlite_path = str(tmp_path / "test.sqlite3")
    # 直接 new 一个 MultiAgentStore，避免 close-then-reuse 引用旧 conn
    return ma_store.MultiAgentStore()


def test_inbox_delivery_and_unread(tmp_path):
    store = _fresh_store(tmp_path)
    agents = [
        AgentSpec(name="Planner", role="planner", goal="plan", watched_message_types=[MessageType.USER_REQUEST]),
        AgentSpec(name="Coder", role="coder", goal="code", watched_message_types=[MessageType.PLAN]),
    ]
    room = TeamRoom.create(task_id="t1", config=TeamRunConfig(goal="g", team_name="sw"), team_spec=TeamSpec(name="sw", description="d", agents=agents), store=store)
    msg = AgentMessage(
        id=make_message_id(),
        task_id="t1", room_id=room.room_id,
        from_agent="system",
        visibility=MessageVisibility.BROADCAST,
        message_type=MessageType.USER_REQUEST,
        content="goal",
    )
    room.publish(msg)

    inbox = AgentInbox(store=store, room_id=room.room_id, task_id="t1")
    unread_planner = inbox.list_unread("Planner")
    unread_coder = inbox.list_unread("Coder")
    assert len(unread_planner) == 1
    assert unread_planner[0].message_type == MessageType.USER_REQUEST
    assert len(unread_coder) == 0  # coder 不订阅 USER_REQUEST


def test_mark_read_after_processed(tmp_path):
    store = _fresh_store(tmp_path)
    agents = [
        AgentSpec(name="Planner", role="planner", goal="plan", watched_message_types=[MessageType.USER_REQUEST]),
    ]
    room = TeamRoom.create(task_id="t2", config=TeamRunConfig(goal="g", team_name="sw"), team_spec=TeamSpec(name="sw", description="d", agents=agents), store=store)
    room.send_system_message("goal", message_type=MessageType.USER_REQUEST)

    inbox = AgentInbox(store=store, room_id=room.room_id, task_id="t2")
    unread = inbox.list_unread("Planner")
    assert len(unread) == 1
    inbox.mark_all_read("Planner")
    unread = inbox.list_unread("Planner")
    assert len(unread) == 0


def test_inbox_relevant_context_sorting(tmp_path):
    store = _fresh_store(tmp_path)
    agents = [
        # Planner 订阅这三个类型，确保所有消息都能进 inbox
        AgentSpec(name="Planner", role="planner", goal="plan",
                  watched_message_types=[MessageType.USER_REQUEST, MessageType.CRITIQUE,
                                         MessageType.REVIEW_REQUEST, MessageType.QUESTION]),
    ]
    room = TeamRoom.create(task_id="t3", config=TeamRunConfig(goal="g", team_name="sw"), team_spec=TeamSpec(name="sw", description="d", agents=agents), store=store)
    # publish several messages
    for content, mt, requires in [
        ("normal", MessageType.QUESTION, False),
        ("must reply", MessageType.REVIEW_REQUEST, True),
        ("critique content", MessageType.CRITIQUE, False),
    ]:
        room.publish(AgentMessage(
            id=make_message_id(),
            task_id="t3", room_id=room.room_id,
            from_agent="ReviewerAgent",
            visibility=MessageVisibility.BROADCAST,
            message_type=mt,
            content=content,
            requires_response=requires,
        ))

    from app.multiagent.inbox import AgentInbox
    inbox = AgentInbox(store=store, room_id=room.room_id, task_id="t3")
    context = inbox.get_relevant_context("Planner", max_items=10)
    assert "must reply" in context
    # 要求响应优先
    must_idx = context.find("must reply")
    normal_idx = context.find("normal")
    assert must_idx < normal_idx
