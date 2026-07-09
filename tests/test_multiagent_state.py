"""SharedTeamState 测试。"""

from app.multiagent.state import (
    SharedTeamState,
    TeamIssue,
    TeamDecision,
    TeamArtifactRef,
    TeamPhase,
    IssueSeverity,
    IssueStatus,
)


def test_initial_state():
    s = SharedTeamState(room_id="r1", task_id="t1", goal="实现功能")
    assert s.phase == TeamPhase.CREATED
    assert s.current_round == 0
    assert s.review_status is None
    assert s.to_prompt_context() is not None


def test_update_phase():
    s = SharedTeamState(room_id="r1", task_id="t1")
    assert s.update_phase(TeamPhase.PLANNING) is True
    assert s.phase == TeamPhase.PLANNING
    # 重复 set 返回 False
    assert s.update_phase(TeamPhase.PLANNING) is False


def test_issues():
    s = SharedTeamState(room_id="r1", task_id="t1")
    issue = TeamIssue(id="i1", title="缺配置", severity=IssueSeverity.HIGH)
    s.add_issue(issue)
    assert len(s.open_issues()) == 1
    s.resolve_issue("i1")
    assert len(s.open_issues()) == 0
    # 不存在的 id
    assert s.resolve_issue("nosuch") is False


def test_blocking_issue_check():
    s = SharedTeamState(room_id="r1", task_id="t1")
    s.add_issue(TeamIssue(id="i1", title="阻塞", severity=IssueSeverity.BLOCKER))
    assert s.has_open_blocking_issues() is True
    s.resolve_issue("i1")
    assert s.has_open_blocking_issues() is False


def test_decisions():
    s = SharedTeamState(room_id="r1", task_id="t1")
    d = TeamDecision(id="d1", title="用方案A", decided_by="Planner")
    s.add_decision(d)
    assert len(s.decisions) == 1
    # 重复 id 不添加
    s.add_decision(d)
    assert len(s.decisions) == 1


def test_artifacts():
    s = SharedTeamState(room_id="r1", task_id="t1")
    a1 = TeamArtifactRef(path="/workspace/plan.md", role="plan", produced_by="Planner")
    a2 = TeamArtifactRef(path="/workspace/code.py", role="code", produced_by="Coder")
    s.add_artifact(a1)
    s.add_artifact(a2)
    assert len(s.artifacts) == 2
    # 同 path 覆盖
    a1_new = TeamArtifactRef(path="/workspace/plan.md", role="plan_v2", produced_by="Planner")
    s.add_artifact(a1_new)
    assert len(s.artifacts) == 2
    assert s.artifacts[0].role == "plan_v2"


def test_steps():
    s = SharedTeamState(room_id="r1", task_id="t1")
    s.mark_step_done("设计架构")
    assert "设计架构" in s.completed_steps
    s.mark_step_done("设计架构")  # 不重复
    assert len(s.completed_steps) == 1
    s.add_blocking_step("实现逻辑")
    assert "实现逻辑" in s.blocked_steps
    s.mark_step_done("实现逻辑")
    assert "实现逻辑" not in s.blocked_steps


def test_open_questions():
    s = SharedTeamState(room_id="r1", task_id="t1")
    s.add_open_question("如何做权限？")
    assert len(s.open_questions) == 1
    s.resolve_open_question("如何做权限？")
    assert len(s.open_questions) == 0


def test_to_prompt_context():
    s = SharedTeamState(room_id="r1", task_id="t1", goal="测试目标")
    s.update_phase(TeamPhase.EXECUTING)
    s.mark_step_done("步骤1")
    context = s.to_prompt_context()
    assert "测试目标" in context
    assert "executing" in context
    assert "步骤1" in context


# ===== P0-2 Artifact Ownership 扩展测试 =====


def test_artifact_version_increments_on_same_path():
    """同 path 复用 add_artifact 时 version 自动 +1。"""
    s = SharedTeamState(room_id="r1", task_id="t1")
    a1 = TeamArtifactRef(path="/ws/main.py", role="code", produced_by="Coder")
    s.add_artifact(a1)
    assert s.artifacts[0].version == 1

    a2 = TeamArtifactRef(path="/ws/main.py", role="code", produced_by="Coder")
    updated = s.add_artifact(a2)
    assert updated.version == 2


def test_artifact_produced_by_preserved_on_update():
    """同 path 更新时首创建者 produced_by 不变。"""
    s = SharedTeamState(room_id="r1", task_id="t1")
    s.add_artifact(TeamArtifactRef(path="/ws/x.py", role="code", produced_by="Coder"))
    # 假设另一 Agent 改了同一文件
    s.add_artifact(TeamArtifactRef(path="/ws/x.py", role="code", produced_by="Tester"))
    assert s.artifacts[0].produced_by == "Coder"  # 首创建者保留
    assert s.artifacts[0].updated_by == "Tester"  # 最后修改者记录


def test_artifact_mark_reviewed():
    """mark_artifact_reviewed 设置 reviewed_by / status / reviewed_at。"""
    s = SharedTeamState(room_id="r1", task_id="t1")
    s.add_artifact(TeamArtifactRef(path="/ws/x.py", role="code", produced_by="Coder"))
    ok = s.mark_artifact_reviewed("/ws/x.py", reviewed_by="ReviewerAgent", status="approved")
    assert ok is True
    assert s.artifacts[0].reviewed_by == "ReviewerAgent"
    assert s.artifacts[0].status == "approved"
    assert s.artifacts[0].reviewed_at is not None


def test_artifact_mark_reviewed_unknown_path():
    """不存在的 artifact mark_reviewed 返回 False。"""
    s = SharedTeamState(room_id="r1", task_id="t1")
    assert s.mark_artifact_reviewed("/no/exist.py", "X") is False


def test_artifact_message_id_linking():
    """带 message_id 关联的 artifact 可追溯关联消息。"""
    s = SharedTeamState(room_id="r1", task_id="t1")
    s.add_artifact(TeamArtifactRef(
        path="/ws/x.py",
        role="code",
        produced_by="Coder",
        message_id="msg_123",
    ))
    assert s.artifacts[0].message_id == "msg_123"

