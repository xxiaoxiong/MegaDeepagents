"""ReviewRepairLoop 测试。"""

from app.multiagent.messages import AgentMessage, MessageType, MessageVisibility, make_message_id
from app.multiagent.review_repair import ReviewRepairLoop, ReviewResult
from app.multiagent.state import SharedTeamState, TeamPhase, IssueStatus


def _state(room_id="r1", task_id="t1"):
    return SharedTeamState(room_id=room_id, task_id=task_id, goal="test")


class _FakeRoom:
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


def test_parse_review_result_json_passed():
    msg = AgentMessage(
        id=make_message_id(), task_id="t1", room_id="r1",
        from_agent="ReviewerAgent",
        visibility=MessageVisibility.BROADCAST,
        message_type=MessageType.REVIEW_RESULT,
        content='{"passed": true, "issues": [], "required_fix_owner": null}',
    )
    result = ReviewRepairLoop.parse_review_result(msg)
    assert result.passed is True
    assert len(result.issues) == 0


def test_parse_review_result_json_failed():
    msg = AgentMessage(
        id=make_message_id(), task_id="t1", room_id="r1",
        from_agent="ReviewerAgent",
        visibility=MessageVisibility.BROADCAST,
        message_type=MessageType.REVIEW_RESULT,
        content='{"passed": false, "issues": [{"severity": "high", "problem": "缺测试"}], "required_fix_owner": "Coder"}',
    )
    result = ReviewRepairLoop.parse_review_result(msg)
    assert result.passed is False
    assert len(result.issues) == 1
    assert result.required_fix_owner == "Coder"


def test_parse_review_result_text():
    """文本启发式判定。"""
    msg = AgentMessage(
        id=make_message_id(), task_id="t1", room_id="r1",
        from_agent="ReviewerAgent",
        visibility=MessageVisibility.BROADCAST,
        message_type=MessageType.REVIEW_RESULT,
        content="审核通过，Review Passed",
    )
    result = ReviewRepairLoop.parse_review_result(msg)
    assert result.passed is True


def test_process_review_passed():
    loop = ReviewRepairLoop(max_cycles=3)
    state = _state()
    room = _FakeRoom()
    result = ReviewResult(passed=True)
    msgs = loop.process_review_result(result, state, room)
    assert state.review_status == "passed"
    assert state.phase == TeamPhase.FINALIZING
    assert len(msgs) == 0


def test_process_review_failed_first_cycle():
    loop = ReviewRepairLoop(max_cycles=3)
    state = _state()
    room = _FakeRoom()
    result = ReviewResult(passed=False, issues=[
        {"severity": "high", "problem": "缺测试", "evidence": [{"kind": "no_tests", "detail": "no tests found"}]},
    ], required_fix_owner="Coder")
    msgs = loop.process_review_result(result, state, room)
    assert state.review_status == "failed"
    assert state.phase == TeamPhase.REPAIRING
    assert len(msgs) == 1
    assert msgs[0].message_type == MessageType.CRITIQUE
    assert "缺测试" in msgs[0].content
    assert len(state.issues) == 1
    assert state.issues[0].status == IssueStatus.OPEN


def test_process_review_exceeds_max_cycles():
    loop = ReviewRepairLoop(max_cycles=1)
    state = _state()
    room = _FakeRoom()
    result = ReviewResult(passed=False, issues=[
        {"severity": "high", "problem": "仍有问题"},
    ], required_fix_owner="Coder")
    loop.process_review_result(result, state, room)  # cycle 1
    result2 = ReviewResult(passed=False, issues=[
        {"severity": "high", "problem": "还是有问题"},
    ], required_fix_owner="Coder")
    msgs2 = loop.process_review_result(result2, state, room)  # cycle 2 > max
    assert state.review_status == "max_retries_exceeded"
    # 因为超了 max，不再生成 critique 消息
    assert len(msgs2) == 0


def test_issue_registration_in_state():
    loop = ReviewRepairLoop(max_cycles=3)
    state = _state()
    room = _FakeRoom()
    result = ReviewResult(passed=False, issues=[
        {"severity": "blocker", "problem": "空指针风险", "evidence": [{"kind": "code", "detail": "line 42"}]},
        {"severity": "medium", "problem": "缺注释", "evidence": []},
    ], required_fix_owner="Coder")
    loop.process_review_result(result, state, room)
    assert len(state.issues) == 2
    assert state.issues[0].severity.value == "blocker"
