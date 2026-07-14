"""Regression coverage for restart-safe TASK_TEAM execution.

These tests use the production SQLite-backed control plane.  They deliberately
reset process singletons between the write and the restore steps, which is the
failure mode a real API worker restart exposes.
"""
from __future__ import annotations

import asyncio


def test_mailbox_message_is_injected_into_the_target_workers_assignment():
    """A delivered user message must change the next real task assignment."""
    from app.multiagent.agent_registry import AgentRegistry
    from app.multiagent.mailbox import MailboxMessage, get_mailbox, reset_mailbox
    from app.multiagent.parallel_scheduler import ParallelTeamScheduler
    from app.multiagent.task_board import TaskBoard
    from app.multiagent.task_graph import TaskGraph, TaskNode

    reset_mailbox()
    board = TaskBoard()
    registry = AgentRegistry()
    agent = registry.create_agent(
        profile_id="coder", name="Coder", role="coder", team_id="team",
        run_id="run_message", capabilities=["coding"], workspace_root=".",
    )
    graph = TaskGraph(root_task_id="write")
    graph.add_node(TaskNode(id="write", title="write", objective="write code", required_capabilities=["coding"]))
    ParallelTeamScheduler.sync_from_task_graph(graph, board, "run_message")
    assert get_mailbox().send(MailboxMessage(
        message_id="msg_user", from_agent_id="user", to_agent_id=agent.agent_id,
        run_id="run_message", title="priority", content="Use Python 3.12 typing.",
    ))

    class AssignmentRecordingExecutor:
        def __init__(self):
            self.messages = []

        def execute_task(self, _dag, _task_id, task_input):
            self.messages = task_input.get("mailbox_messages", [])
            from app.multiagent.scheduler import TaskResult
            return TaskResult(task_id="write", success=True)

    scheduler = ParallelTeamScheduler("run_message", task_graph=graph, max_rounds=2)
    scheduler.board = board
    scheduler.registry = registry
    executor = AssignmentRecordingExecutor()
    assert asyncio.run(scheduler.run(executor)).status == "completed"
    assert [message["content"] for message in executor.messages] == ["Use Python 3.12 typing."]


def test_task_team_verifier_repair_creates_and_executes_repair_task(tmp_path):
    """A verifier repair verdict must create work, not terminate as completed."""
    from app.multiagent.orchestrator import SimpleOrchestrator
    from app.multiagent.scheduler import TaskResult
    from app.multiagent.task_graph import TaskGraph, TaskNode
    from app.multiagent.team_run_context import TeamRunContext
    from app.multiagent.task_board import get_task_board
    from app.multiagent.verifier import ValidationResult, Verdict
    from app.multiagent.artifact import ArtifactStore

    store = ArtifactStore(root_path=str(tmp_path / "run"))

    class RepairThenPassVerifier:
        def __init__(self):
            self.calls = 0
            self.artifact_store = store

        def validate(self, **_kwargs):
            self.calls += 1
            return ValidationResult(verdict=Verdict.REPAIR if self.calls == 1 else Verdict.PASS)

    class SuccessfulWorker:
        def execute_task(self, _dag, task_id, task_input):
            artifact_ids = []
            if "__repair_v" in task_id:
                artifact = store.create(
                    run_id=task_input["run_id"], task_id=task_id, type="patch",
                    relative_path=f"tasks/{task_id}/repair.py", content="fixed = True\n",
                    produced_by="Coder",
                )
                artifact_ids.append(artifact.id)
            return TaskResult(task_id=task_id, success=True, artifact_ids=artifact_ids)

    graph = TaskGraph(root_task_id="implement")
    graph.add_node(TaskNode(id="implement", title="implement", objective="write feature", required_capabilities=["coding"]))
    ctx = TeamRunContext.create("write feature", workspace_root=str(tmp_path / "run"))
    result = SimpleOrchestrator(
        executor=SuccessfulWorker(), verifier=RepairThenPassVerifier(), ctx=ctx, max_repair_rounds=2,
    ).run("write feature", mode_override="full_multi", task_graph=graph)

    assert result.status == "completed"
    tasks = get_task_board().list_by_run(ctx.run_id)
    assert any("__repair_v" in task.task_id and task.status.value == "succeeded" for task in tasks)


def test_task_board_restores_and_requeues_interrupted_work_after_singleton_reset():
    from app.multiagent.task_board import get_task_board, reset_task_board, BoardTaskStatus

    board = get_task_board()
    board.create_task("implement", "run_restart", "Implement", "write the feature")
    assert board.claim("implement", "agent_before_restart", run_id="run_restart").success
    assert board.start("implement", "agent_before_restart", run_id="run_restart")

    # Simulate a process restart: no in-memory board survives.
    reset_task_board()
    restored = get_task_board()
    assert restored.restore_run("run_restart") == 1
    assert restored.get("implement", run_id="run_restart").status == BoardTaskStatus.RUNNING

    # A lease held by a dead process must not strand the task forever.
    assert restored.prepare_for_resume("run_restart") == 1
    task = restored.get("implement", run_id="run_restart")
    assert task.status == BoardTaskStatus.PENDING
    assert task.claimed_by is None
    assert task.last_error == "interrupted_before_resume"


def test_resume_restores_stable_agent_identity_and_rehydrates_task_board():
    from app.multiagent.phase_g_store import get_agent_run_history
    from app.multiagent.resume_coordinator import ResumeCoordinator
    from app.multiagent.agent_registry import get_agent_registry, reset_agent_registry
    from app.multiagent.task_board import get_task_board, reset_task_board, BoardTaskStatus

    history = get_agent_run_history()
    history.upsert_agent_instance(
        agent_id="agent_stable", team_id="team", run_id="run_resume",
        profile_id="coder", name="Coder", role="coder",
        session_id="session_stable", thread_id="thread_stable",
        checkpoint_namespace="team:run_resume:coder", status="idle",
        capabilities=["coding"],
    )
    get_task_board().create_task("unfinished", "run_resume", "Unfinished", "continue work")

    # Clear both runtime registries exactly as a new worker process would.
    reset_agent_registry()
    reset_task_board()

    result = ResumeCoordinator().resume("run_resume")
    assert result.resumed_agents == 1
    restored_agent = get_agent_registry().get("agent_stable")
    assert restored_agent is not None
    assert restored_agent.session_id == "session_stable"
    assert restored_agent.thread_id == "thread_stable"
    assert get_task_board().get("unfinished", run_id="run_resume").status == BoardTaskStatus.PENDING


def test_facade_cold_resume_reconstructs_context_and_continues_execution(monkeypatch, tmp_path):
    """Resume is not successful until it schedules the persisted run again."""
    from app.multiagent.phase_g_store import get_agent_run_history
    from app.multiagent.team_runtime import TeamRuntimeFacade
    from app.multiagent.team_run_context import TeamRunMode

    get_agent_run_history().save_team_run(
        run_id="run_cold", goal="finish persisted work", team_id="software_dev_team",
        mode="task_team", workspace_root=str(tmp_path / "workspace"), status="interrupted",
        max_rounds=7, review_required=True,
    )
    runtime = TeamRuntimeFacade()
    continued: list[tuple[str, str, int]] = []

    async def continue_task_team(ctx, goal, team_name, max_rounds, review_required, *, resume=False):
        assert resume is True
        continued.append((ctx.run_id, goal, max_rounds))
        from app.multiagent.agent_spec import TeamRunResult
        return TeamRunResult(task_id=ctx.run_id, status="completed", final_output="done")

    monkeypatch.setattr(runtime, "_run_task_team", continue_task_team)
    assert asyncio.run(runtime.resume_run("run_cold"))
    assert continued == [("run_cold", "finish persisted work", 7)]
    run = asyncio.run(runtime.get_run("run_cold"))
    assert run["status"] == "completed"


def test_resume_restores_full_task_graph_not_only_board_projection(tmp_path):
    """A restart must retain contracts and graph version needed by verification."""
    from app.multiagent.phase_g_store import get_agent_run_history
    from app.multiagent.team_runtime import TeamRuntimeFacade
    from app.multiagent.task_graph import OutputContract, TaskGraph, TaskNode

    graph = TaskGraph(root_task_id="implement", version=7)
    graph.add_node(TaskNode(
        id="implement", title="Implement", objective="produce a checked module",
        dependencies=[], required_capabilities=["coding", "testing"],
        output_contract=OutputContract(
            artifact_type="code", required_artifacts=["module.py"],
            acceptance_criteria=["pytest -q passes"], allow_parallel=False,
        ),
        metadata={"plan_revision": "v7"},
    ))
    history = get_agent_run_history()
    history.save_task_graph("run_graph_restore", graph.model_dump(mode="json"))

    restored = TeamRuntimeFacade()._task_graph_from_persisted_board("run_graph_restore")
    assert restored is not None
    node = restored.nodes["implement"]
    assert restored.version == graph.version
    assert node.output_contract.required_artifacts == ["module.py"]
    assert node.output_contract.acceptance_criteria == ["pytest -q passes"]
    assert node.required_capabilities == ["coding", "testing"]
    assert node.metadata["plan_revision"] == "v7"


def test_resume_rehydrates_unconsumed_mailbox_messages_after_process_restart():
    """A wake-up message sent before a restart must reach the restored teammate."""
    from app.multiagent.agent_registry import reset_agent_registry
    from app.multiagent.mailbox import MailboxMessage, get_mailbox, reset_mailbox
    from app.multiagent.phase_g_store import get_agent_run_history
    from app.multiagent.resume_coordinator import ResumeCoordinator
    from app.multiagent.task_board import reset_task_board

    history = get_agent_run_history()
    history.upsert_agent_instance(
        agent_id="agent_mail_restore", team_id="team", run_id="run_mail_restore",
        profile_id="coder", name="Coder", role="coder", session_id="session",
        thread_id="thread", checkpoint_namespace="team:run_mail_restore:coder",
        status="idle", capabilities=["coding"],
    )
    assert get_mailbox().send(MailboxMessage(
        message_id="mail_after_restart", from_agent_id="user",
        to_agent_id="agent_mail_restore", run_id="run_mail_restore",
        title="continue", content="Prioritize the failing test.",
    ))

    reset_agent_registry()
    reset_task_board()
    reset_mailbox()
    ResumeCoordinator().resume("run_mail_restore")

    received = get_mailbox().receive("agent_mail_restore")
    assert [message.content for message in received] == ["Prioritize the failing test."]


def test_agent_registry_persists_each_lease_transition_without_team_builder():
    """A claim is durable even when it was made by the scheduler, not TeamBuilder."""
    from app.multiagent.agent_registry import AgentRegistry
    from app.multiagent.phase_g_store import get_agent_run_history

    registry = AgentRegistry()
    agent = registry.create_agent(
        profile_id="coder", name="Coder", role="coder", team_id="team",
        run_id="run_registry_persist", capabilities=["coding"], workspace_root=".",
    )
    reserved = registry.reserve_idle_agent("run_registry_persist", {"coding"}, "task_a")
    assert reserved is not None

    stored = get_agent_run_history().get_agent_instance(agent.agent_id)
    assert stored is not None
    assert stored["status"] == "claiming"
    assert stored["current_task_id"] == "task_a"

    assert registry.release_reservation(agent.agent_id, "task_a")
    stored = get_agent_run_history().get_agent_instance(agent.agent_id)
    assert stored["status"] == "idle"
    assert stored["current_task_id"] is None
