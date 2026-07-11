"""TerminationChecker 测试。"""

from app.multiagent.agent_spec import TeamSpec
from app.multiagent.messages import AgentMessage, MessageType, make_message_id, MessageVisibility
from app.multiagent.state import SharedTeamState, TeamPhase, TeamIssue, IssueSeverity
from app.multiagent.termination import TerminationChecker


def _spec(max_rounds=20, review=True, max_cycles=3):
    return TeamSpec(name="t", description="d", agents=[], max_rounds=max_rounds,
                    review_required=review, max_review_cycles=max_cycles)


def _checker(spec=None, stale=2):
    return TerminationChecker(team_spec=spec or _spec(), max_stale_rounds=stale)


def _state(room_id="r1", task_id="t1", phase=TeamPhase.CREATED, round=0, max_rounds=20):
    return SharedTeamState(room_id=room_id, task_id=task_id, goal="test",
                           phase=phase, current_round=round, max_rounds=max_rounds)


def _noop_msg():
    return AgentMessage(id=make_message_id(), task_id="t1", room_id="r1", from_agent="system",
                        visibility=MessageVisibility.BROADCAST, message_type=MessageType.NO_OP, content="")


def test_not_terminated_by_default():
    c = _checker()
    d = c.check(state=_state(), recent_messages=[], round_count=0)
    assert not d.should_terminate


def test_completed_phase_is_terminal():
    c = _checker()
    s = _state(phase=TeamPhase.COMPLETED)
    d = c.check(state=s, recent_messages=[], round_count=0)
    assert d.should_terminate
    assert "completed" in d.reason


def test_max_rounds_terminates():
    c = _checker(_spec(max_rounds=5))
    d = c.check(state=_state(round=5, max_rounds=5), recent_messages=[], round_count=5)
    assert d.should_terminate
    assert "max_rounds" in d.reason


def test_final_message_terminates():
    c = _checker()
    s = _state()
    s.final_output = "最终输出完成"
    d = c.check(state=s, recent_messages=[], round_count=1)
    assert d.should_terminate


def test_final_type_message_terminates():
    c = _checker()
    final_msg = AgentMessage(id=make_message_id(), task_id="t1", room_id="r1", from_agent="Finalizer",
                             visibility=MessageVisibility.BROADCAST, message_type=MessageType.FINAL, content="done")
    d = c.check(state=_state(), recent_messages=[final_msg], round_count=1)
    assert d.should_terminate


def test_stale_noop_terminates():
    c = _checker(stale=2)
    s = _state()
    # 2 rounds of noop
    d1 = c.check(state=s, recent_messages=[_noop_msg()], round_count=1)
    assert not d1.should_terminate
    d2 = c.check(state=s, recent_messages=[_noop_msg()], round_count=2)
    assert d2.should_terminate
    assert "stale" in d2.reason


def test_error_message_terminates():
    c = _checker()
    err_msg = AgentMessage(id=make_message_id(), task_id="t1", room_id="r1", from_agent="system",
                           visibility=MessageVisibility.BROADCAST, message_type=MessageType.ERROR, content="error")
    d = c.check(state=_state(), recent_messages=[err_msg], round_count=1)
    assert d.should_terminate


def test_cancel_requested_terminates():
    c = _checker()
    s = _state()
    s.metadata["cancel_requested"] = True
    d = c.check(state=s, recent_messages=[], round_count=0)
    assert d.should_terminate
    assert "cancel" in d.reason


def test_review_passed_terminates():
    c = _checker()
    s = _state(phase=TeamPhase.REVIEWING)
    s.review_status = "passed"
    d = c.check(state=s, recent_messages=[], round_count=1)
    assert d.should_terminate
    assert "review_passed" in d.reason


def test_max_review_cycles_terminates():
    c = _checker(_spec(max_cycles=2))
    s = _state(phase=TeamPhase.REVIEWING)
    s.review_cycles = 5
    d = c.check(state=s, recent_messages=[], round_count=1)
    assert d.should_terminate
    assert "max_review_cycles" in d.reason


# ===== 需求 5：终止语义 =====


def test_max_rounds_should_be_incomplete_not_completed():
    """达到 max_rounds 时终态应为 INCOMPLETE，不是 COMPLETED。

    回归保护：旧实现把 max_rounds 当作 COMPLETED 是错的——只有显式产出
    final_output（final_message / review_passed）才算完成。
    """
    c = _checker(_spec(max_rounds=5))
    s = _state(phase=TeamPhase.EXECUTING, round=5, max_rounds=5)
    s.final_output = None
    s.review_status = None
    d = c.check(state=s, recent_messages=[], round_count=5)
    assert d.should_terminate
    assert "max_rounds" in d.reason
    # 关键：达到 max_rounds 不应误判为已通过评审或产最终答案
    assert "review_passed" not in d.reason
    # phase 仍可推进到 INCOMPLETE（由 runner/executor 决定），但不应被改为 COMPLETED
    assert s.phase != TeamPhase.COMPLETED


def test_max_rounds_explicit_incomplete_phase():
    """TerminationChecker 在 max_rounds 路径可写入 INCOMPLETE 终态。"""
    c = _checker(_spec(max_rounds=3))
    s = _state(phase=TeamPhase.EXECUTING, round=3, max_rounds=3)
    s.final_output = None
    d = c.check(state=s, recent_messages=[], round_count=3)
    assert d.should_terminate
    # INCOMPLETE 阶段本身应被识别为终止态
    d2 = c.check(state=_state(phase=TeamPhase.INCOMPLETE, round=3, max_rounds=3),
                 recent_messages=[], round_count=3)
    assert d2.should_terminate


def test_final_message_only_completes_if_explicit():
    """只有显式产出 final_output / FINAL 消息才是 COMPLETED，max_rounds + final 双触发优先完成。"""
    c = _checker()
    s = _state(phase=TeamPhase.EXECUTING, round=10, max_rounds=10)
    s.final_output = "最终交付"
    d = c.check(state=s, recent_messages=[], round_count=10)
    assert d.should_terminate
    # 优先 final_message 而非 max_rounds
    assert "final" in d.reason or "max_rounds" in d.reason

