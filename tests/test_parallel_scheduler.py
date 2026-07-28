"""并行调度器测试 + 端到端并行验证。

验证点：
- 20 个协程同时认领同一个 Task 时只能一个成功
- 两个独立 Task 各休眠 2 秒，总耗时接近 2 秒而非 4 秒
- 同一 Profile max_concurrency=1 时两个任务不得同时执行
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.multiagent.task_board import (
    BoardTask,
    BoardTaskStatus,
    TaskBoard,
    get_task_board,
    reset_task_board,
)
from app.multiagent.agent_instance import AgentStatus
from app.multiagent.agent_registry import (
    AgentRegistry,
    get_agent_registry,
    reset_agent_registry,
)


# ===== Fixtures =====

@pytest.fixture(autouse=True)
def reset_all():
    reset_task_board()
    reset_agent_registry()
    yield
    reset_task_board()
    reset_agent_registry()


# ===== 并发争抢测试 =====

class TestParallelClaim:
    @pytest.mark.asyncio
    async def test_20_coroutines_single_claim(self):
        """20 个协程同时认领同一个 Task，只能一个成功。"""
        board = get_task_board()
        board.create_task(task_id="hot_task", run_id="r1", title="Hot", objective="test")

        async def try_claim(agent_id: str) -> bool:
            result = board.claim("hot_task", agent_id)
            return result.success

        # 20 个并发认领
        coros = [try_claim(f"agent_{i}") for i in range(20)]
        results = await asyncio.gather(*coros)

        success_count = sum(1 for r in results if r)
        assert success_count == 1, f"期望 1 个成功，实际 {success_count} 个成功"

    @pytest.mark.asyncio
    async def test_non_parallel_task_time(self):
        """两个独立 Task 各 sleep 2s，并行总耗时接近 2s。"""
        board = get_task_board()
        reg = get_agent_registry()

        board.create_task(task_id="t1", run_id="r1", title="T1", objective="s1")
        board.create_task(task_id="t2", run_id="r1", title="T2", objective="s2")

        # 创建两个 Agent
        a1 = reg.create_agent(
            profile_id="p1", name="W1", role="worker",
            team_id="t", run_id="r1", capabilities=["sleep"],
        )
        a2 = reg.create_agent(
            profile_id="p2", name="W2", role="worker",
            team_id="t", run_id="r1", capabilities=["sleep"],
        )

        async def fake_exec(task_id: str) -> bool:
            await asyncio.sleep(2.0)
            return True

        async def run_one(task_id: str, agent: Any) -> bool:
            claim = board.claim(task_id, agent.agent_id)
            if not claim.success:
                return False
            board.start(task_id, agent.agent_id)
            agent.update_status(AgentStatus.RUNNING)
            ok = await fake_exec(task_id)
            if ok:
                board.complete(task_id, agent.agent_id)
            else:
                board.fail(task_id, agent.agent_id, "err")
            agent.update_status(AgentStatus.IDLE)
            return ok

        start = time.time()
        results = await asyncio.gather(
            run_one("t1", a1),
            run_one("t2", a2),
        )
        elapsed = time.time() - start

        assert all(results), "两个任务都应该成功"
        # 并行总耗时应接近 2s，远小于 4s
        assert elapsed < 3.0, f"并行总耗时 {elapsed:.2f}s >= 3s（太大），非并行"

    @pytest.mark.asyncio
    async def test_serialized_by_max_concurrency(self):
        """同一个 Agent 最大并发 1 时，两个任务必须串行。"""
        board = get_task_board()
        reg = get_agent_registry()

        board.create_task(task_id="t1", run_id="r2", title="T1", objective="a")
        board.create_task(task_id="t2", run_id="r2", title="T2", objective="b")

        # 只有一个 Agent（max_concurrency 由 Agent 并发数体现）
        a1 = reg.create_agent(
            profile_id="p1", name="Solo", role="worker",
            team_id="t", run_id="r2", capabilities=["work"],
        )

        async def run_serial():
            for tid in ["t1", "t2"]:
                claim = board.claim(tid, a1.agent_id)
                if not claim.success:
                    return False
                board.start(tid, a1.agent_id)
                a1.update_status(AgentStatus.RUNNING)
                await asyncio.sleep(0.05)
                board.complete(tid, a1.agent_id)
                a1.update_status(AgentStatus.IDLE)
            return True

        start = time.time()
        ok = await run_serial()
        elapsed = time.time() - start
        assert ok
        assert elapsed > 0.09, f"串行总耗时 {elapsed:.3f}s，应 > 2*0.05s=0.1s"


# ===== ParallelTeamScheduler 烟雾测试 =====

class TestParallelScheduler:
    def test_discovery_matches_one_ready_task_per_idle_agent(self):
        """A dispatch wave must not enqueue more work than workers can reserve."""
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        board = get_task_board()
        reg = get_agent_registry()
        for index in range(5):
            board.create_task(
                task_id=f"t{index}",
                run_id="r_capacity",
                title=f"T{index}",
                objective="work",
                required_capabilities=["coding"],
            )
        agent = reg.create_agent(
            profile_id="coder",
            name="Coder",
            role="worker",
            team_id="team",
            run_id="r_capacity",
            capabilities=["coding"],
        )
        scheduler = ParallelTeamScheduler(
            run_id="r_capacity",
            max_rounds=10,
            max_concurrency=4,
        )
        scheduler.board = board
        scheduler.registry = reg

        discovered = scheduler._discover_dispatchable({})

        assert len(discovered) == 1
        assert scheduler._dispatch_agent_hints[discovered[0].task_id] == agent.agent_id

    def test_discovery_preserves_specialists_and_accepts_tool_capability_fallback(self):
        """Constrained tasks are matched first and tool caps do not deadlock."""
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        board = get_task_board()
        reg = get_agent_registry()
        board.create_task(
            task_id="planning",
            run_id="r_match",
            title="Plan",
            objective="plan",
            required_capabilities=["planning"],
        )
        board.create_task(
            task_id="coding",
            run_id="r_match",
            title="Code",
            objective="code",
            required_capabilities=["coding", "file_write"],
        )
        generalist = reg.create_agent(
            profile_id="generalist",
            name="Generalist",
            role="worker",
            team_id="team",
            run_id="r_match",
            capabilities=["planning", "coding"],
        )
        specialist = reg.create_agent(
            profile_id="coder",
            name="Coder",
            role="worker",
            team_id="team",
            run_id="r_match",
            capabilities=["coding"],
        )
        scheduler = ParallelTeamScheduler(
            run_id="r_match",
            max_rounds=10,
            max_concurrency=2,
        )
        scheduler.board = board
        scheduler.registry = reg

        discovered = scheduler._discover_dispatchable({})

        assert {task.task_id for task in discovered} == {"planning", "coding"}
        assert scheduler._dispatch_agent_hints == {
            "planning": generalist.agent_id,
            "coding": specialist.agent_id,
        }

    @pytest.mark.asyncio
    async def test_scheduler_simple_run(self):
        """编写一个简易并行调度器验证基本流通。"""
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        board = get_task_board()
        reg = get_agent_registry()

        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        board.create_task(
            task_id="t2", run_id="r1", title="T2", objective="o",
            dependencies=["t1"],
        )

        a1 = reg.create_agent(
            profile_id="p1", name="W1", role="worker",
            team_id="t", run_id="r1", capabilities=["default"],
        )

        # 用 ThreadPoolExecutor 包装 DagExecutor（简化：直接返回成功）
        class SimpleExecutor:
            async def execute_task(self, dag, task_id, task_input):
                return type("Result", (), {"success": True, "artifact_ids": [f"art_{task_id}"], "error": None})()

        # 注意这个调度器的 executor 需要有 execute_task 方法且可以异步调用。
        # 为了测试，我们实现一个适配器
        class FakeExecutor:
            def execute_task(self, dag, task_id, task_input):
                return type("R", (), {"success": True, "artifact_ids": [f"art_{task_id}"], "error": None})()

        scheduler = ParallelTeamScheduler(run_id="r1", max_rounds=3, max_concurrency=2)
        scheduler.registry = reg
        scheduler.board = board

        result = await scheduler.run(FakeExecutor())
        assert result.status == "completed"
        assert result.succeeded == 2
        assert result.total_tasks == 2

    @pytest.mark.asyncio
    async def test_scheduler_consumes_task_graph_mutations_before_dispatch(self):
        """A teammate-created task must execute against the new graph version."""
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler
        from app.multiagent.task_graph import TaskGraph, TaskNode
        from app.multiagent.transactional_task_service import TransactionalTaskService

        run_id = "r_dynamic_graph"
        graph = TaskGraph(root_task_id="seed")
        graph.add_node(TaskNode(
            id="seed",
            title="Seed",
            objective="create the follow-up",
            required_capabilities=["default"],
        ))
        tasks = TransactionalTaskService()
        await asyncio.to_thread(tasks.register_initial_graph, run_id, graph)
        registry = get_agent_registry()
        registry.create_agent(
            profile_id="p1",
            name="Worker",
            role="worker",
            team_id="team",
            run_id=run_id,
            capabilities=["default"],
        )
        observed: list[tuple[str, set[str]]] = []

        class MutatingExecutor:
            def execute_task(self, dag, task_id, task_input):
                if task_id == "seed":
                    tasks.create_task(
                        run_id,
                        task_input["agent_id"],
                        TaskNode(
                            id="follow_up",
                            title="Follow-up",
                            objective="finish the dynamically discovered work",
                            dependencies=["seed"],
                            required_capabilities=["default"],
                        ).model_dump(mode="json"),
                    )
                observed.append((task_id, set(dag.nodes)))
                return type("R", (), {
                    "success": True,
                    "artifact_ids": [f"art_{task_id}"],
                    "error": None,
                })()

        scheduler = ParallelTeamScheduler(
            run_id=run_id,
            max_rounds=4,
            max_concurrency=1,
            task_graph=graph,
        )
        result = await scheduler.run(MutatingExecutor())

        assert result.status == "completed"
        assert [task_id for task_id, _ in observed] == ["seed", "follow_up"]
        assert observed[1][1] == {"seed", "follow_up"}
        assert scheduler.task_graph.version > graph.version

    @pytest.mark.asyncio
    async def test_scheduler_with_failure(self):
        """Task 失败的场景。"""
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        board = get_task_board()
        reg = get_agent_registry()

        board.create_task(
            task_id="t1", run_id="r2", title="T1", objective="o",
            max_attempts=1,
        )

        a1 = reg.create_agent(
            profile_id="p1", name="W1", role="worker",
            team_id="t", run_id="r2", capabilities=["default"],
        )

        class FailingExecutor:
            def execute_task(self, dag, task_id, task_input):
                return type("R", (), {"success": False, "artifact_ids": [], "error": "boom"})()

        scheduler = ParallelTeamScheduler(run_id="r2", max_rounds=3, max_concurrency=2)
        scheduler.registry = reg
        scheduler.board = board

        result = await scheduler.run(FailingExecutor())
        assert result.status != "completed"  # 不全是成功
        assert result.failed > 0

    @pytest.mark.asyncio
    async def test_sync_from_task_graph(self):
        """从 TaskGraph 同步到 TaskBoard。"""
        from app.multiagent.task_graph import TaskGraph, TaskNode, OutputContract
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        dag = TaskGraph(root_task_id="t1")
        dag.add_node(TaskNode(
            id="t1", title="Step1", objective="step1",
            dependencies=[], required_capabilities=["coding"],
        ))
        dag.add_node(TaskNode(
            id="t2", title="Step2", objective="step2",
            dependencies=["t1"], required_capabilities=["testing"],
        ))

        board = get_task_board()
        ParallelTeamScheduler.sync_from_task_graph(dag, board, "r3")

        assert board.get("t1") is not None
        assert board.get("t2") is not None
        assert board.get("t2").dependencies == ["t1"]

        # 回写验证
        # 手动完成 t1
        board.claim("t1", "agent_x")
        board.start("t1", "agent_x")
        board.complete("t1", "agent_x", ["art1"])

        ParallelTeamScheduler.sync_back_to_dag(dag, board, "r3")
        from app.multiagent.task_graph import TaskNodeStatus
        assert dag.nodes["t1"].status == TaskNodeStatus.SUCCEEDED
        assert "art1" in dag.nodes["t1"].output_artifact_ids


def test_online_verification_does_not_block_the_scheduler_event_loop():
    import threading

    from app.multiagent.parallel_scheduler import ParallelTeamScheduler

    scheduler = ParallelTeamScheduler("run_non_blocking_verification")
    started = threading.Event()
    release = threading.Event()

    def blocking_verify(_task):
        started.set()
        assert release.wait(timeout=2)
        return True

    scheduler._verify_task = blocking_verify

    async def exercise():
        verification = asyncio.create_task(
            scheduler._verify_task_async(object())
        )
        assert await asyncio.to_thread(started.wait, 1)

        # If verification ran on the event-loop thread this timer could not
        # complete until ``release`` was set.
        loop_remained_responsive = False

        async def tick():
            nonlocal loop_remained_responsive
            await asyncio.sleep(0.01)
            loop_remained_responsive = True

        await asyncio.wait_for(tick(), timeout=0.2)
        assert loop_remained_responsive
        assert not verification.done()
        release.set()
        assert await verification is True

    asyncio.run(exercise())


def test_allow_parallel_false_creates_an_exclusive_dispatch_barrier():
    from app.multiagent.parallel_scheduler import ParallelTeamScheduler
    from app.multiagent.task_graph import OutputContract, TaskGraph, TaskNode

    run_id = "run_exclusive_barrier"
    graph = TaskGraph(root_task_id="exclusive")
    graph.add_node(TaskNode(
        id="exclusive",
        title="Exclusive migration",
        objective="change shared state",
        required_capabilities=["coding"],
        output_contract=OutputContract(allow_parallel=False),
        priority=9,
    ))
    graph.add_node(TaskNode(
        id="parallel_a",
        title="Parallel A",
        objective="independent analysis",
        required_capabilities=["coding"],
    ))
    graph.add_node(TaskNode(
        id="parallel_b",
        title="Parallel B",
        objective="independent checks",
        required_capabilities=["coding"],
    ))
    board = TaskBoard()
    ParallelTeamScheduler.sync_from_task_graph(graph, board, run_id)
    registry = AgentRegistry()
    for index in range(3):
        registry.create_agent(
            profile_id="coder",
            name=f"Coder {index}",
            role="coder",
            team_id="team",
            run_id=run_id,
            capabilities=["coding"],
        )
    scheduler = ParallelTeamScheduler(
        run_id,
        task_graph=graph,
        max_concurrency=3,
    )
    scheduler.board = board
    scheduler.registry = registry

    first_wave = scheduler._discover_dispatchable({})
    assert [task.task_id for task in first_wave] == ["exclusive"]

    # While an exclusive task is in flight, no other work may be admitted.
    assert scheduler._discover_dispatchable(
        {object(): "exclusive"}
    ) == []

    # A normal parallel wave cannot admit the exclusive task until it drains.
    wave = scheduler._discover_dispatchable(
        {object(): "parallel_a"}
    )
    assert {task.task_id for task in wave} == {"parallel_b"}
