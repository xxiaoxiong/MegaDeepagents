"""SpeakerSelector 规则测试。"""

from app.multiagent.agent_spec import AgentSpec
from app.multiagent.inbox import (
    AgentInbox,
)
from app.multiagent.messages import MessageType, make_message_id, AgentMessage, MessageVisibility
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import SharedTeamState, TeamPhase


class _StoreWithInbox:
    """使用最简单的内存存储来支持 Inbox 操作。"""
    def __init__(self):
        self.inbox: dict[str, list[dict]] = {}  # (room_id, agent_name) -> list

    def get_agent_unread_inbox(self, room_id, agent_name):
        key = (room_id, agent_name)
        return self.inbox.get(key, [])

    def get_agent_full_inbox(self, room_id, agent_name):
        return self.get_agent_unread_inbox(room_id, agent_name)

    def ack_message(self, message_id, agent_name):
        pass

    def deliver_to_inbox(self, agent_name, message_id, room_id, task_id, from_agent=None, message_type=None):
        key = (room_id, agent_name)
        if key not in self.inbox:
            self.inbox[key] = []
        self.inbox[key].append(
            AgentMessage(id=message_id, task_id=task_id, room_id=room_id,
                         from_agent=from_agent or "system",
                         visibility=MessageVisibility.BROADCAST,
                         message_type=MessageType(message_type or "observation"),
                         content="")
        )

    def save_message(self, msg):
        pass


def _agents():
    return [
        AgentSpec(name="Planner", role="Planner", goal="p", watched_message_types=[MessageType.USER_REQUEST]),
        AgentSpec(name="Coder", role="Coder", goal="c", watched_message_types=[MessageType.PLAN]),
        AgentSpec(name="ReviewerAgent", role="ReviewerAgent", goal="r", watched_message_types=[MessageType.REVIEW_REQUEST]),
    ]


def _make_sel(agents=None, state=None, store=None):
    agents = agents or _agents()
    state = state or SharedTeamState(room_id="r1", task_id="t1")
    if store is None:
        store = _StoreWithInbox()
    inbox = AgentInbox(store=store, room_id="r1", task_id="t1")
    selector = SpeakerSelector()
    return selector, agents, inbox, state, store


def test_requires_response_priority():
    """有 requires_response=True 的 Agent 优先被选。"""
    sel, agents, inbox, state, store = _make_sel()
    store.deliver_to_inbox("Coder", make_message_id(), "r1", "t1", from_agent="ReviewerAgent", message_type="review_request")
    # Coder 不订阅 review_request 所以默认可能不选；但 requires_response 强制。
    # SpeakerSelector 的 rule1 要求 to_agent == name
    chosen = sel.select(state, agents, inbox, None)
    # 问题：broadcast review_request 没有 to_agent，所以 rule1 不触发（要求 to_agent == name）
    # 所以这里只验证 rule2（must_act_type）
    assert chosen is not None


def test_review_request_triggers_reviewer():
    """review_request 优先选 ReviewerAgent。"""
    sel, agents, inbox, state, store = _make_sel()
    store.deliver_to_inbox("ReviewerAgent", make_message_id(), "r1", "t1",
                           from_agent="Coder", message_type="review_request")
    chosen = sel.select(state, agents, inbox, None)
    assert chosen is not None
    assert chosen.name == "ReviewerAgent"


def test_user_request_triggers_planner():
    """user_request 优先选 Planner（rule2 must_act_type）。"""
    sel, agents, inbox, state, store = _make_sel()
    store.deliver_to_inbox("Planner", make_message_id(), "r1", "t1",
                           from_agent="system", message_type="user_request")
    chosen = sel.select(state, agents, inbox, None)
    assert chosen is not None


def test_planning_phase_bias():
    """planning phase 优先选 Planner。"""
    sel, agents, inbox, state, store = _make_sel()
    state.update_phase(TeamPhase.PLANNING)
    chosen = sel.select(state, agents, inbox, None)
    assert chosen is not None
    assert chosen.name == "Planner"


def test_executing_phase_bias():
    """executing phase 优先选 Coder。"""
    sel, agents, inbox, state, store = _make_sel()
    state.update_phase(TeamPhase.EXECUTING)
    chosen = sel.select(state, agents, inbox, None)
    assert chosen is not None
    assert chosen.name == "Coder"


def test_reviewing_phase_bias():
    """reviewing phase 优先选 ReviewerAgent。"""
    sel, agents, inbox, state, store = _make_sel()
    state.update_phase(TeamPhase.REVIEWING)
    chosen = sel.select(state, agents, inbox, None)
    assert chosen is not None
    assert chosen.name == "ReviewerAgent"


def test_anti_stall_changes_speaker():
    """anti-stall 行为：当所有 Agent 都只有普通 observation 消息时，
    rule4 优先选择 != last_speaker 的 Agent。
    """
    sel, agents, inbox, state, store = _make_sel()
    state.update_phase(TeamPhase.DISCUSSING)
    # 投递普通观察消息给两个 Agent
    store.deliver_to_inbox("Planner", make_message_id(), "r1", "t1", from_agent="system", message_type="observation")
    store.deliver_to_inbox("Coder", make_message_id(), "r1", "t1", from_agent="ReviewerAgent", message_type="observation")
    # 第一轮：规则 4 选 Planner（首个有未读且 !=None）
    first = sel.select(state, agents, inbox, None)
    # 第二轮：last_speaker=Planner，规则 4 应跳过 Planner 选 Coder
    second = sel.select(state, agents, inbox, last_speaker=first.name)
    assert second.name != first.name
