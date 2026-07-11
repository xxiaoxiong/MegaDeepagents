"""B2-B5 集成 smoke 测试：并行、冲突仲裁、跨任务记忆、团队仪表盘 API。

设计原则：
- 不触发真实 LLM（用 mock build_model + actions_to_messages 走通主流程）
- 不外发 LangSmith（默认 offline）
- 全部在 fixtures 内隔离
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.multiagent.agent_spec import TeamSpec
from app.multiagent.conflict_resolver import (
    ConflictResolver,
    ConflictType,
    Resolution,
)
from app.multiagent.parallel_runner import (
    ParallelExecutionPlan,
    execute_parallel,
    plan_parallel_round,
)
from app.multiagent.state import SharedTeamState, TeamPhase


def _make_state(phase: TeamPhase = TeamPhase.EXECUTING) -> SharedTeamState:
    """构造一份最小可用的 SharedTeamState，用于冲突解析 / 并行测试。"""
    st = SharedTeamState(
        goal="?", team_name="t", max_rounds=5,
        room_id="room_test", task_id="task_test",
    )
    st.update_phase(phase)
    return st


# ---------- B2: parallel_runner ----------


class TestB2ParallelRunner:
    def test_plan_skips_when_max_concurrent_is_one(self):
        """max_concurrent=1 → 返回 None，主循环走串行路径。"""
        from app.multiagent.agent_spec import AgentSpec

        agents = [
            AgentSpec(name="Planner", role="planner", watched_message_types=[],
                      system_prompt="", goal=""),
            AgentSpec(name="Coder", role="coder", watched_message_types=[],
                      system_prompt="", goal=""),
        ]
        inbox = MagicMock()
        inbox.list_unread.return_value = [MagicMock()]
        inbox.get_relevant_context.return_value = "ctx"
        plan = plan_parallel_round(
            phase=TeamPhase.EXECUTING,
            agents=agents,
            inbox=inbox,
            last_speaker=None,
            primary_speaker=agents[0],
            max_concurrent=1,
            primary_unread=[],
            primary_inbox_context="",
        )
        assert plan is None

    def test_plan_skips_non_parallel_phase(self):
        from app.multiagent.agent_spec import AgentSpec

        agents = [
            AgentSpec(name="Planner", role="planner", watched_message_types=[],
                      system_prompt="", goal=""),
            AgentSpec(name="Coder", role="coder", watched_message_types=[],
                      system_prompt="", goal=""),
        ]
        inbox = MagicMock()
        inbox.list_unread.return_value = [MagicMock()]
        inbox.get_relevant_context.return_value = "ctx"
        plan = plan_parallel_round(
            phase=TeamPhase.PLANNING,
            agents=agents,
            inbox=inbox,
            last_speaker=None,
            primary_speaker=agents[0],
            max_concurrent=3,
            primary_unread=[MagicMock()],
            primary_inbox_context="",
        )
        assert plan is None

    def test_execute_parallel_two_agents_runs_concurrently(self):
        """execute_parallel 在多 Agent 时用 ThreadPoolExecutor 并行调用 adapter.run。"""
        from app.multiagent.agent_spec import AgentSpec

        state = _make_state(TeamPhase.EXECUTING)
        agents = [
            AgentSpec(name="Planner", role="planner", watched_message_types=[],
                      system_prompt="", goal="", allowed_tools=[]),
            AgentSpec(name="Coder", role="coder", watched_message_types=[],
                      system_prompt="", goal="", allowed_tools=[]),
        ]
        plan = ParallelExecutionPlan(
            primary=agents[0],
            primary_unread=[],
            primary_inbox_context="",
            secondary=[agents[1]],
            secondary_unread={"Coder": []},
            secondary_context={"Coder": ""},
        )
        adapter = MagicMock()
        # 第一次调用（primary）返回 [act_a]，第二次（secondary）返回 [act_b]
        adapter.run.side_effect = lambda agent, inbox_messages, shared_state: [
            {"type": "no_op", "content": f"from {agent.name}"}
        ]
        results = execute_parallel(plan=plan, adapter=adapter,
                                   shared_state_snapshot=state, team_agents=agents)
        assert set(results.keys()) == {"Planner", "Coder"}
        assert results["Planner"][0]["content"] == "from Planner"
        assert results["Coder"][0]["content"] == "from Coder"
        assert adapter.run.call_count == 2


# ---------- B5: ConflictResolver LLM 仲裁 ----------


class TestB5ConflictResolver:
    def test_reviewer_veto_rule_resolves(self):
        """Reviewer 投不通过，按规则 engine 解析（不走 LLM）。"""
        state = _make_state()
        resolver = ConflictResolver(state=state)
        result = resolver.resolve(
            conflict_type=ConflictType.REVIEW_DISAGREEMENT,
            description="Reviewer 不通过",
            positions=[
                {"agent": "ReviewerAgent", "position": False, "reason": "不通过：缺测试"},
                {"agent": "Coder", "position": True, "reason": "测试已写过"},
            ],
            context={},
        )
        assert result.resolved is True
        assert result.decision == "reviewer_veto"
        assert result.escalate_to_hitl is False

    def test_priority_conflict_safety_first(self):
        state = _make_state()
        resolver = ConflictResolver(state=state)
        result = resolver.resolve(
            conflict_type=ConflictType.PRIORITY_CONFLICT,
            description="安全 vs 性能",
            positions=[
                {"agent": "Coder", "position": "性能优先", "reason": "重构提升 QPS"},
                {"agent": "Planner", "position": "安全优先", "reason": "存在 SQL 注入安全隐患"},
            ],
        )
        assert result.resolved is True
        # Planner 的 reason 包含"安全"二字 → 优先级规则触发"安全 > 性能"
        assert "安全" in result.decision or "涉及安全" in result.decision

    def test_llm_arbitration_fallback_when_rules_cannot(self):
        """规则引擎兜不住（无 Reviewer 制裁且不属于已知类型 OTHER）：B5 LLM 仲裁生效。"""
        state = _make_state()
        resolver = ConflictResolver(state=state)

        fake_response = MagicMock()
        fake_response.content = json.dumps({
            "decision": "采用 Coder 方案",
            "reason": "测试已存在，Reviewer 未参与",
        })

        with patch("app.llm_factory.build_model", return_value=MagicMock(invoke=MagicMock(return_value=fake_response))):
            result = resolver.resolve(
                conflict_type=ConflictType.OTHER,
                description="两方对立，规则无法裁决",
                positions=[
                    {"agent": "Coder", "position": "无需改", "reason": "测试已有"},
                    {"agent": "Planner", "position": "需要补测", "reason": "测试覆盖不足"},
                ],
                context={"phase": "executing"},
            )
        assert result.resolved is True
        assert "Coder" in result.decision
        assert result.escalate_to_hitl is False
        # 一条 TeamDecision 应被记录
        assert any(d.decided_by.startswith("Planner") or "arbitration" in d.decided_by for d in state.decisions)

    def test_llm_unavailable_escalates_to_hitl(self, caplog):
        """LLM 不可用时降级到 HITL。"""
        state = _make_state()
        resolver = ConflictResolver(state=state)

        with patch("app.llm_factory.build_model", side_effect=RuntimeError("no key")):
            result = resolver.resolve(
                conflict_type=ConflictType.OTHER,
                description="",
                positions=[{"agent": "?", "position": "?"}],
                context={"phase": "executing"},
            )
        assert result.resolved is False
        assert result.escalate_to_hitl is True
        # 在 state 中创建 blocking issue
        assert any(i.owner is None and i.severity.value == "high" for i in state.issues)


# ---------- B4: routes_team 新端点 ----------


class TestB4TeamDashboardEndpoints:
    def test_list_available_teams(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.get("/teams")
        assert resp.status_code == 200
        teams = resp.json()
        assert isinstance(teams, list)
        names = [t["name"] for t in teams]
        assert "software_dev_team" in names

    def test_list_team_tasks_endpoint_exists(self):
        """GET /team-tasks 应可访问（即便空也会返回 200 + []）。"""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.get("/team-tasks?limit=5")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
