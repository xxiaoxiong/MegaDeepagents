from __future__ import annotations

from app.infrastructure.database.run_store import get_agent_run_history
from app.multiagent.artifact import ArtifactStore, ArtifactType
from app.domain.tasks.models import TaskExecutionResult as TaskResult
from app.multiagent.task_graph import OutputContract, TaskGraph, TaskNode
from app.multiagent.team_run_context import TeamRunContext, TeamRunMode
from app.multiagent.verifier import EvidenceRef, ValidationResult, Verdict
from app.runtime.root_graph.graph import GovernedRunGraph, run_governed


class RecordingExecutor:
    """Test-only worker seam; production always injects DeepAgentExecutor."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self.calls: list[str] = []

    def execute_task(self, task_graph, task_id, task_input):
        self.calls.append(task_id)
        artifact = self.store.create(
            run_id=task_input["run_id"],
            task_id=task_id,
            type=ArtifactType.DOCUMENT,
            relative_path=f"tasks/{task_id}/result.txt",
            content=f"verified output for {task_id}",
            produced_by=task_input["agent_id"],
        )
        return TaskResult(
            task_id=task_id,
            success=True,
            artifact_ids=[artifact.id],
            attempted=True,
        )


class PassVerifier:
    def __init__(self, store: ArtifactStore) -> None:
        self.artifact_store = store
        self.calls = 0

    def validate(self, *, goal, artifacts, checks=None):
        self.calls += 1
        assert artifacts
        return ValidationResult(
            verdict=Verdict.PASS,
            scores={"evidence": 1.0},
            evidence=[EvidenceRef(source="test-evidence", content=goal)],
            summary="evidence verified",
        )


class FinalRepairThenPassVerifier(PassVerifier):
    def validate(self, *, goal, artifacts, checks=None):
        self.calls += 1
        assert artifacts
        # Call 1 verifies the original worker task. Call 2 is the whole-run
        # gate and requests repair. The repair task and second whole-run gate
        # then pass.
        verdict = Verdict.REPAIR if self.calls == 2 else Verdict.PASS
        return ValidationResult(
            verdict=verdict,
            scores={"evidence": 0.0 if verdict == Verdict.REPAIR else 1.0},
            summary="repair required" if verdict == Verdict.REPAIR else "evidence verified",
        )


class FinalReplanThenPassVerifier(PassVerifier):
    def validate(self, *, goal, artifacts, checks=None):
        self.calls += 1
        assert artifacts
        verdict = Verdict.REPLAN if self.calls == 2 else Verdict.PASS
        return ValidationResult(
            verdict=verdict,
            scores={"evidence": 0.0 if verdict == Verdict.REPLAN else 1.0},
            summary="new plan required" if verdict == Verdict.REPLAN else "verified",
        )


def _context(tmp_path, goal: str) -> TeamRunContext:
    ctx = TeamRunContext.create(
        goal,
        team_name="software_dev_team",
        mode=TeamRunMode.TASK_TEAM,
        workspace_root=str(tmp_path / "workspace"),
    )
    get_agent_run_history().save_team_run(
        run_id=ctx.run_id,
        goal=goal,
        team_id=ctx.team_id,
        mode=ctx.mode.value,
        workspace_root=ctx.workspace_root,
        status="running",
        max_rounds=10,
        review_required=False,
        metadata={"requested_mode": "team"},
    )
    return ctx


def test_single_and_team_share_the_production_root_graph(tmp_path):
    single_ctx = _context(tmp_path / "single", "Summarize the supplied notes")
    single_store = ArtifactStore(single_ctx.workspace_root)
    single_executor = RecordingExecutor(single_store)
    single_verifier = PassVerifier(single_store)

    def planner_must_not_run(goal, feedback):
        raise AssertionError("explicit single mode must not call the team planner")

    single = run_governed(
        goal=single_ctx.user_goal,
        requested_mode="single",
        planner=planner_must_not_run,
        executor=single_executor,
        verifier=single_verifier,
        ctx=single_ctx,
        max_rounds=10,
    )

    assert single.status == "completed"
    assert single.mode == "single"
    assert single.total_tasks == 1
    assert single.succeeded_tasks == 1
    assert single.verification_verdict == "pass"

    team_ctx = _context(tmp_path / "team", "Analyze and summarize two sources")
    team_store = ArtifactStore(team_ctx.workspace_root)
    team_executor = RecordingExecutor(team_store)
    team_verifier = PassVerifier(team_store)

    def team_planner(goal, feedback):
        graph = TaskGraph(root_task_id="source-a")
        for task_id in ("source-a", "source-b"):
            graph.add_node(TaskNode(
                id=task_id,
                title=task_id,
                objective=f"{goal}: {task_id}",
                required_capabilities=["summarization"],
                output_contract=OutputContract(
                    artifact_type="document",
                    description="A durable evidence artifact",
                ),
            ))
        return graph

    team = run_governed(
        goal=team_ctx.user_goal,
        requested_mode="team",
        planner=team_planner,
        executor=team_executor,
        verifier=team_verifier,
        ctx=team_ctx,
        max_rounds=10,
    )

    assert team.status == "completed"
    assert team.mode == "team"
    assert team.total_tasks == 2
    assert team.succeeded_tasks == 2
    assert set(team_executor.calls) == {"source-a", "source-b"}
    assert team_verifier.calls >= 3
    events = get_agent_run_history().list_event_envelopes(team_ctx.run_id, 0, 100)
    assert any(item["event_type"] == "root_graph:run_finalized" for item in events)


def test_legacy_task_artifacts_migrate_without_colliding_with_v3(tmp_path):
    import sqlite3

    from app.core.config import settings
    from app.infrastructure.database.connection import close_connection, get_connection

    close_connection()
    legacy_path = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(legacy_path)
    raw.execute(
        """CREATE TABLE artifacts (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           task_id TEXT NOT NULL,
           path TEXT NOT NULL,
           name TEXT NOT NULL,
           size_bytes INTEGER DEFAULT 0,
           created_at TEXT NOT NULL)"""
    )
    raw.execute(
        "INSERT INTO artifacts(task_id,path,name,size_bytes,created_at) VALUES(?,?,?,?,?)",
        ("old-task", "old.txt", "old.txt", 3, "2026-01-01T00:00:00"),
    )
    raw.commit()
    raw.close()

    settings.sqlite_path = str(legacy_path)
    conn = get_connection()
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(task_artifacts)")
    }
    assert {"task_id", "path", "name"}.issubset(columns)
    assert conn.execute("SELECT COUNT(*) FROM task_artifacts").fetchone()[0] == 1

    from app.multiagent.store import _get_conn

    v3_conn = _get_conn()
    v3_columns = {
        row["name"] for row in v3_conn.execute("PRAGMA table_info(artifacts)")
    }
    assert {"artifact_id", "run_id", "relative_path"}.issubset(v3_columns)


def test_whole_run_verifier_creates_and_executes_a_real_repair_task(tmp_path):
    ctx = _context(tmp_path, "Implement a verified change")
    store = ArtifactStore(ctx.workspace_root)
    executor = RecordingExecutor(store)
    verifier = FinalRepairThenPassVerifier(store)

    def planner(goal, feedback):
        graph = TaskGraph(root_task_id="implement")
        graph.add_node(TaskNode(
            id="implement",
            title="Implement",
            objective=goal,
            required_capabilities=["coding"],
            output_contract=OutputContract(
                artifact_type="document",
                description="Verified implementation evidence",
            ),
        ))
        return graph

    result = run_governed(
        goal=ctx.user_goal,
        requested_mode="team",
        planner=planner,
        executor=executor,
        verifier=verifier,
        ctx=ctx,
        max_rounds=10,
    )

    assert result.status == "completed"
    repair_ids = [task_id for task_id in executor.calls if "__repair_v" in task_id]
    assert len(repair_ids) == 1
    assert result.succeeded_tasks == 1
    from app.multiagent.task_board import get_task_board

    board = get_task_board().list_by_run(ctx.run_id)
    assert any(
        task.task_id == repair_ids[0] and task.status.value == "succeeded"
        for task in board
    )


def test_replan_atomically_materializes_and_executes_the_revised_graph(tmp_path):
    from app.multiagent.task_board import get_task_board

    ctx = _context(tmp_path, "Replan when the first approach is insufficient")
    store = ArtifactStore(ctx.workspace_root)
    executor = RecordingExecutor(store)
    verifier = FinalReplanThenPassVerifier(store)
    planner_calls = 0

    def planner(goal, feedback):
        nonlocal planner_calls
        planner_calls += 1
        task_id = "first_approach" if planner_calls == 1 else "revised_approach"
        graph = TaskGraph(root_task_id=task_id)
        graph.add_node(TaskNode(
            id=task_id,
            title=task_id,
            objective=f"{goal}: {task_id}",
            required_capabilities=["coding"],
            output_contract=OutputContract(
                artifact_type="document",
                description="verified evidence",
            ),
        ))
        return graph

    result = run_governed(
        goal=ctx.user_goal,
        requested_mode="team",
        planner=planner,
        executor=executor,
        verifier=verifier,
        ctx=ctx,
        max_rounds=10,
    )

    assert result.status == "completed"
    assert planner_calls == 2
    assert executor.calls == ["first_approach", "revised_approach"]
    board = get_task_board().list_by_run(ctx.run_id)
    old = next(task for task in board if task.task_id == "first_approach")
    revised = next(task for task in board if task.task_id == "revised_approach")
    assert old.metadata["superseded_by_plan_revision"]
    assert revised.status.value == "succeeded"
    persisted = get_agent_run_history().load_task_graph(ctx.run_id)
    assert set(persisted["nodes"]) == {"revised_approach"}
    assert any(
        event["event_type"] == "root_graph:replan_requested"
        for event in get_agent_run_history().list_event_envelopes(ctx.run_id)
    )


def test_incomplete_dispatch_never_reaches_collection_or_verification():
    graph = object.__new__(GovernedRunGraph)

    assert graph._route_after_dispatch({"dispatch_status": "incomplete"}) == "fail"
    assert graph._route_after_dispatch({"dispatch_status": "completed"}) == "collect"


def test_run_verifier_rejects_incomplete_task_board_before_calling_judge(tmp_path):
    from app.multiagent.task_board import get_task_board

    ctx = _context(tmp_path, "Do not accept partial completion")
    task_graph = TaskGraph(root_task_id="unfinished")
    task_graph.add_node(TaskNode(
        id="unfinished",
        title="Unfinished",
        objective="produce complete evidence",
        required_capabilities=["coding"],
    ))
    get_task_board().create_task(
        "unfinished",
        ctx.run_id,
        "Unfinished",
        "produce complete evidence",
        required_capabilities=["coding"],
    )
    verifier = PassVerifier(ArtifactStore(ctx.workspace_root))
    governed = object.__new__(GovernedRunGraph)
    governed.ctx = ctx
    governed.verifier = verifier

    result = governed._verify({
        "goal": ctx.user_goal,
        "task_graph_json": task_graph.model_dump_json(),
    })

    assert result["verification_summary"]["verdict"] == "fail"
    assert result["error"].startswith("tasks_not_succeeded:")
    assert verifier.calls == 0


def test_resume_delivers_the_human_decision_through_langgraph_command():
    from types import SimpleNamespace

    from langgraph.types import Command

    class CompiledGraph:
        def __init__(self):
            self.value = None
            self.config = None

        def invoke(self, value, *, config):
            self.value = value
            self.config = config
            return {
                "status": "succeeded",
                "mode": "team",
                "task_graph_version": 0,
                "dispatch_rounds": 0,
                "verification_summary": {},
            }

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    compiled = CompiledGraph()
    connection = Connection()
    governed = object.__new__(GovernedRunGraph)
    governed.ctx = SimpleNamespace(
        run_id="run_resume_command",
        checkpoint_namespace="team:run_resume_command",
    )
    governed._compiled = compiled
    governed._checkpoint_connection = connection
    decision = {"decision": "deny", "feedback": "Use a safer approach."}

    result = governed.invoke(
        goal="resume",
        requested_mode="team",
        resume=True,
        resume_decision=decision,
    )

    assert result.status == "completed"
    assert isinstance(compiled.value, Command)
    assert compiled.value.resume == decision
    assert compiled.config["configurable"]["thread_id"] == "run_resume_command"
    assert connection.closed
