"""MultiAgent MessageBus 测试。"""

import json

from app.multiagent.bus import MessageBus
from app.multiagent.agent_spec import AgentSpec, AgentSubscription
from app.multiagent.messages import AgentMessage, MessageType, MessageVisibility, make_message_id


def _make_msg(task_id="t1", room_id="r1", from_agent="system", msg_type=MessageType.USER_REQUEST,
              vis=MessageVisibility.BROADCAST, to_agent=None, content="hello", requires_response=False):
    return AgentMessage(
        id=make_message_id(),
        task_id=task_id,
        room_id=room_id,
        from_agent=from_agent,
        to_agent=to_agent,
        visibility=vis,
        message_type=msg_type,
        content=content,
        requires_response=requires_response,
    )


class _InMemoryStore:
    """用于测试的内存 store 替代品。"""
    def __init__(self):
        self.inbox: dict[str, list] = {}  # (room_id, agent_name) -> list of dict

    def save_message(self, message):
        pass

    def deliver_to_inbox(self, agent_name, message_id, room_id, task_id, from_agent=None, message_type=None):
        key = (room_id, agent_name)
        if key not in self.inbox:
            self.inbox[key] = []
        self.inbox[key].append({
            "message_id": message_id,
            "from_agent": from_agent,
            "message_type": message_type,
            "is_read": False,
        })

    def get_agent_inbox(self, room_id, agent_name):
        # not used in tests
        return []

    def ack_message(self, message_id, agent_name):
        pass


def _make_bus(agents=None):
    if agents is None:
        agents = [
            AgentSpec(name="Planner", role="planner", goal="plan",
                      watched_message_types=[MessageType.USER_REQUEST, MessageType.PLAN]),
            AgentSpec(name="Coder", role="coder", goal="code",
                      watched_message_types=[MessageType.PLAN, MessageType.DELEGATION]),
            AgentSpec(name="ReviewerAgent", role="reviewer", goal="review",
                      watched_message_types=[MessageType.REVIEW_REQUEST, MessageType.ARTIFACT_CREATED]),
        ]
    return MessageBus(room_id="r1", task_id="t1", agents=agents, store=_InMemoryStore())


def test_direct_message_only_delivers_to_target():
    """direct message 只进入指定 Agent inbox。"""
    bus = _make_bus()
    msg = _make_msg(vis=MessageVisibility.DIRECT, to_agent="Coder", msg_type=MessageType.DELEGATION)
    bus.publish(msg)

    assert len(bus._transcript) == 1
    # In memory store check
    assert len(bus.store.inbox.get(("r1", "Coder"), [])) == 1
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 0
    assert len(bus.store.inbox.get(("r1", "ReviewerAgent"), [])) == 0


def test_broadcast_uses_subscription():
    """broadcast 根据 watched_message_types 分发。"""
    bus = _make_bus()
    msg = _make_msg(vis=MessageVisibility.BROADCAST, msg_type=MessageType.PLAN, content="计划内容")
    bus.publish(msg)

    # Planner 订阅了 PLAN, Coder 也订阅了 PLAN, Reviewer 没有
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 1
    assert len(bus.store.inbox.get(("r1", "Coder"), [])) == 1
    assert len(bus.store.inbox.get(("r1", "ReviewerAgent"), [])) == 0


def test_broadcast_review_request():
    """broadcast review_request 只给 ReviewerAgent。"""
    bus = _make_bus()
    msg = _make_msg(vis=MessageVisibility.BROADCAST, msg_type=MessageType.REVIEW_REQUEST)
    bus.publish(msg)

    assert len(bus.store.inbox.get(("r1", "ReviewerAgent"), [])) == 1
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 0


def test_system_delivers_to_all():
    """system visibility 投递给所有 Agent。"""
    bus = _make_bus()
    msg = _make_msg(vis=MessageVisibility.SYSTEM, msg_type=MessageType.STATE_UPDATE)
    bus.publish(msg)

    for agent_name in ("Planner", "Coder", "ReviewerAgent"):
        assert len(bus.store.inbox.get(("r1", agent_name), [])) == 1


def test_no_unwatched_delivery():
    """Agent 不订阅的消息类型不投递。"""
    bus = _make_bus()
    msg = _make_msg(vis=MessageVisibility.BROADCAST, msg_type=MessageType.TEST_RESULT)
    bus.publish(msg)

    for agent_name in ("Planner", "Coder", "ReviewerAgent"):
        assert len(bus.store.inbox.get(("r1", agent_name), [])) == 0


def test_subscription_refines_watched():
    """AgentSubscription 精细控制订阅。"""
    agents = [
        AgentSpec(name="Planner", role="p", goal="p",
                  subscription=AgentSubscription(
                      message_types=[MessageType.CRITIQUE],
                      from_agents=["ReviewerAgent"],
                  )),
    ]
    bus = _make_bus(agents)

    # Critique by ReviewerAgent → deliver
    msg1 = _make_msg(from_agent="ReviewerAgent", msg_type=MessageType.CRITIQUE)
    bus.publish(msg1)
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 1

    # Critique_by_wrong_agent → skip
    msg2 = _make_msg(from_agent="Coder", msg_type=MessageType.CRITIQUE)
    bus.publish(msg2)
    # still just 1
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 1


def test_direct_no_to_agent_fallsback():
    """direct 无 to_agent 时 fallback broadcast。"""
    bus = _make_bus([AgentSpec(name="Planner", role="p", goal="p", watched_message_types=[MessageType.USER_REQUEST])])
    msg = AgentMessage(
        id=make_message_id(), task_id="t1", room_id="r1",
        from_agent="system", to_agent=None,
        visibility=MessageVisibility.DIRECT,
        message_type=MessageType.USER_REQUEST,
        content="fallback",
    )
    bus.publish(msg)
    # planner watches USER_REQUEST
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 1


def test_multiple_recipients():
    """direct 支持 list 多接收方。"""
    agents = [
        AgentSpec(name="Planner", role="p", goal="p"),
        AgentSpec(name="Coder", role="c", goal="c"),
    ]
    bus = _make_bus(agents)
    msg = _make_msg(vis=MessageVisibility.DIRECT, to_agent=["Planner", "Coder"], msg_type=MessageType.DELEGATION)
    bus.publish(msg)
    assert len(bus.store.inbox.get(("r1", "Planner"), [])) == 1
    assert len(bus.store.inbox.get(("r1", "Coder"), [])) == 1
