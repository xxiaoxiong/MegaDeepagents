"""Cancellation regressions for the production TASK_TEAM scheduler path."""
from __future__ import annotations

import asyncio
import threading


def test_cancelling_an_active_task_never_verifies_it_as_succeeded():
    """A worker returning after cancel must not turn the run into completed."""
    from app.multiagent.agent_registry import AgentRegistry
    from app.multiagent.parallel_scheduler import ParallelTeamScheduler
    from app.multiagent.task_board import TaskBoard, BoardTaskStatus
    from app.multiagent.task_graph import TaskGraph, TaskNode

    board = TaskBoard()
    registry = AgentRegistry()
    agent = registry.create_agent(
        profile_id="coder", name="Coder", role="coder", team_id="team",
        run_id="run_cancel_active", capabilities=["coding"], workspace_root=".",
    )
    graph = TaskGraph(root_task_id="write")
    graph.add_node(TaskNode(id="write", title="write", objective="write a module", required_capabilities=["coding"]))
    ParallelTeamScheduler.sync_from_task_graph(graph, board, "run_cancel_active")
    cancelled = threading.Event()
    worker_started = threading.Event()

    class CooperativeWorker:
        def execute_task(self, _dag, task_id, task_input):
            worker_started.set()
            assert task_input["cancel_event"].wait(timeout=2)
            # A late worker response is intentionally successful; the runtime
            # must still preserve cancellation rather than accepting it.
            from app.multiagent.scheduler import TaskResult
            return TaskResult(task_id=task_id, success=True)

    scheduler = ParallelTeamScheduler("run_cancel_active", task_graph=graph, max_rounds=3, cancel_event=cancelled)
    scheduler.board = board
    scheduler.registry = registry

    async def run_and_cancel():
        task = asyncio.create_task(scheduler.run(CooperativeWorker()))
        await asyncio.to_thread(worker_started.wait, 1)
        cancelled.set()
        return await task

    result = asyncio.run(run_and_cancel())
    task = board.get("write", run_id="run_cancel_active")
    assert result.status == "cancelled"
    assert task.status == BoardTaskStatus.CANCELLED
    assert task.status != BoardTaskStatus.SUCCEEDED
    assert agent.current_task_id is None


def test_mailbox_waiter_is_woken_by_a_different_thread():
    """API delivery must wake a Scheduler-loop waiter without loop affinity bugs."""
    from app.multiagent.mailbox import Mailbox, MailboxMessage

    mailbox = Mailbox()

    def deliver():
        assert mailbox.send(MailboxMessage(
            message_id="cross_thread", from_agent_id="user", to_agent_id="agent",
            run_id="run", title="wake", content="continue",
        ))

    async def wait_then_deliver():
        sender = threading.Timer(0.05, deliver)
        sender.start()
        message = await mailbox.wait_for_message("agent", timeout=1)
        sender.join()
        return message

    message = asyncio.run(wait_then_deliver())
    assert message is not None
    assert message.content == "continue"


def test_facade_controls_the_same_teammate_lifecycle_as_scheduler_registry(tmp_path):
    from app.multiagent.agent_instance import AgentStatus
    from app.multiagent.agent_registry import get_agent_registry
    from app.multiagent.team_runtime import TeamRuntimeFacade

    runtime = TeamRuntimeFacade()
    ctx = asyncio.run(runtime.create_run("lifecycle", workspace_root=str(tmp_path / "run")))
    agent = get_agent_registry().create_agent(
        profile_id="coder", name="Coder", role="coder", team_id=ctx.team_id,
        run_id=ctx.run_id, capabilities=["coding"], workspace_root=ctx.workspace_root,
    )

    assert asyncio.run(runtime.pause_agent(ctx.run_id, agent.agent_id))
    assert get_agent_registry().get(agent.agent_id).status == AgentStatus.BLOCKED
    assert asyncio.run(runtime.resume_agent(ctx.run_id, agent.agent_id))
    assert get_agent_registry().get(agent.agent_id).status == AgentStatus.IDLE
    assert asyncio.run(runtime.stop_agent(ctx.run_id, agent.agent_id))
    assert get_agent_registry().get(agent.agent_id).status == AgentStatus.STOPPED


def test_cold_cancel_cancels_persisted_board_before_a_future_resume(tmp_path):
    """A non-resident API runtime cannot leave pending durable work alive."""
    from app.multiagent.phase_g_store import get_agent_run_history
    from app.multiagent.task_board import get_task_board, reset_task_board, BoardTaskStatus
    from app.multiagent.team_runtime import TeamRuntimeFacade

    run_id = "run_cold_cancel"
    get_agent_run_history().save_team_run(
        run_id=run_id, goal="do not resume", team_id="software_dev_team",
        mode="task_team", workspace_root=str(tmp_path / "workspace"), status="interrupted",
        max_rounds=3, review_required=True,
    )
    get_task_board().create_task("unfinished", run_id, "Unfinished", "must stop")

    # Simulate a fresh API process: only SQLite remains.
    reset_task_board()
    assert asyncio.run(TeamRuntimeFacade().cancel_run(run_id))
    reset_task_board()
    restored = get_task_board()
    assert restored.restore_run(run_id) == 1
    assert restored.get("unfinished", run_id=run_id).status == BoardTaskStatus.CANCELLED


def test_stopping_one_active_agent_releases_work_for_another_teammate():
    """A stopped worker's late success cannot complete its claimed task."""
    from app.multiagent.agent_instance import AgentStatus
    from app.multiagent.agent_registry import AgentRegistry
    from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
    from app.multiagent.parallel_scheduler import ParallelTeamScheduler
    from app.multiagent.task_board import TaskBoard, BoardTaskStatus
    from app.multiagent.task_graph import TaskGraph, TaskNode

    board = TaskBoard()
    registry = AgentRegistry()
    first = registry.create_agent(
        profile_id="coder", name="First", role="coder", team_id="team",
        run_id="run_stop_agent", capabilities=["coding"], workspace_root=".",
    )
    second = registry.create_agent(
        profile_id="coder", name="Second", role="coder", team_id="team",
        run_id="run_stop_agent", capabilities=["coding"], workspace_root=".",
    )
    graph = TaskGraph(root_task_id="write")
    graph.add_node(TaskNode(id="write", title="write", objective="write module", required_capabilities=["coding"]))
    ParallelTeamScheduler.sync_from_task_graph(graph, board, "run_stop_agent")
    first_started = threading.Event()
    executions: list[str] = []

    class LateSuccessWorker:
        def execute_task(self, _dag, task_id, task_input):
            executions.append(task_input["agent_id"])
            if task_input["agent_id"] == first.agent_id:
                first_started.set()
                assert task_input["cancel_event"].wait(timeout=2)
            from app.multiagent.scheduler import TaskResult
            return TaskResult(task_id=task_id, success=True)

    scheduler = ParallelTeamScheduler("run_stop_agent", task_graph=graph, max_rounds=4)
    scheduler.board = board
    scheduler.registry = registry

    async def run_then_stop():
        running = asyncio.create_task(scheduler.run(LateSuccessWorker()))
        await asyncio.to_thread(first_started.wait, 1)
        assert get_agent_runtime_manager().stop_agent("run_stop_agent", first.agent_id)
        return await running

    result = asyncio.run(run_then_stop())
    assert result.status == "completed"
    assert executions == [first.agent_id, second.agent_id]
    assert registry.get(first.agent_id).status == AgentStatus.STOPPED
    assert board.get("write", run_id="run_stop_agent").status == BoardTaskStatus.SUCCEEDED
