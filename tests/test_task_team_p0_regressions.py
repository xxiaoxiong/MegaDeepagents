"""P0 regression tests for the TASK_TEAM production path.

These tests deliberately exercise the real control-plane classes.  They do
not replace the executor with a fake that ignores the scheduler protocol.
"""
from __future__ import annotations

import asyncio

from app.multiagent.agent_registry import AgentRegistry
from app.multiagent.parallel_scheduler import ParallelTeamScheduler
from app.multiagent.task_board import TaskBoard
from app.multiagent.task_graph import TaskGraph, TaskNode
from app.multiagent.agent_profile import AgentProfile, ToolPolicy
from app.multiagent.artifact import ArtifactStore
from app.multiagent.executor import DeepAgentExecutor, ExecutionContext, TaskAssignment


class DagRecordingExecutor:
    """Protocol-correct executor: production executors require a real DAG."""

    def __init__(self) -> None:
        self.received_dag = None

    def execute_task(self, dag, task_id, task_input):
        from app.multiagent.scheduler import TaskResult

        self.received_dag = dag
        assert dag is not None
        assert task_id in dag.nodes
        return TaskResult(task_id=task_id, success=True, artifact_ids=[])


def test_task_board_keeps_same_local_task_id_isolated_by_run():
    board = TaskBoard()
    board.create_task("task_1", "run_a", "A", "first")
    board.create_task("task_1", "run_b", "B", "second")

    assert board.get("task_1", run_id="run_a").objective == "first"
    assert board.get("task_1", run_id="run_b").objective == "second"
    assert [task.task_id for task in board.list_by_run("run_a")] == ["task_1"]
    assert [task.task_id for task in board.list_by_run("run_b")] == ["task_1"]


def test_parallel_scheduler_passes_real_task_graph_to_executor(monkeypatch):
    board = TaskBoard()
    registry = AgentRegistry()
    registry.create_agent(
        profile_id="coder", name="Coder", role="coder", team_id="team",
        run_id="run", capabilities=["coding"], workspace_root=".",
    )
    dag = TaskGraph(root_task_id="task_1")
    dag.add_node(TaskNode(id="task_1", title="implement", objective="write code",
                           required_capabilities=["coding"]))
    ParallelTeamScheduler.sync_from_task_graph(dag, board, "run")
    monkeypatch.setattr("app.multiagent.parallel_scheduler.get_task_board", lambda: board)
    monkeypatch.setattr("app.multiagent.parallel_scheduler.get_agent_registry", lambda: registry)

    scheduler = ParallelTeamScheduler("run", task_graph=dag, max_rounds=2)
    executor = DagRecordingExecutor()
    result = asyncio.run(scheduler.run(executor))

    assert result.status == "completed"
    assert executor.received_dag is dag


def test_real_deep_agent_executor_registers_written_artifacts(monkeypatch, tmp_path):
    """Exercise DeepAgentExecutor itself, including its artifact loop.

    The model/DeepAgents constructor is substituted only to avoid a network
    model call; the production executor and ArtifactStore are both used.
    """
    class LocalDeepAgent:
        def invoke(self, _payload, config=None):
            target = tmp_path / "tasks" / "write" / "answer.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"def answer(): return 42\n")
            return {"messages": [type("Message", (), {"content": "written"})()]}

    monkeypatch.setattr("deepagents.create_deep_agent", lambda **_kwargs: LocalDeepAgent())
    monkeypatch.setattr("app.llm_factory.build_model_for_policy", lambda _policy: object())

    executor = DeepAgentExecutor(workspace_root=str(tmp_path))
    store = ArtifactStore(root_path=str(tmp_path))
    executor.set_artifact_store(store)
    profile = AgentProfile(
        id="coder", name="Coder", role="coder", capabilities={"coding"},
        tool_policy=ToolPolicy(allowed_tools=["create_file"], deny_all_by_default=True,
                               allow_file_write=True),
    )
    result = executor.execute(
        TaskAssignment(task_id="write", objective="write file", description="write file"),
        profile,
        ExecutionContext(run_id="run_artifacts", workspace_root=str(tmp_path), thread_id="thread_agent"),
    )

    assert result.success is True
    assert len(result.produced_artifact_ids) == 1
    artifact = store.get(result.produced_artifact_ids[0])
    assert artifact is not None
    assert artifact.path == "tasks/write/answer.py"
    assert store.read(artifact.id) == "def answer(): return 42\n"
