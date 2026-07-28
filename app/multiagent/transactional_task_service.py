"""Transactional authority for TaskGraph mutations and TaskBoard creation."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
from app.infrastructure.database.run_store import get_agent_run_history
from app.multiagent.store import _get_conn
from app.multiagent.task_board import BoardTask, get_task_board
from app.multiagent.task_graph import TaskGraph, TaskNode


class TaskGraphMutationType(str, Enum):
    CREATE_TASK = "create_task"
    ADD_DEPENDENCY = "add_dependency"
    UPDATE_TASK = "update_task"
    REPAIR_TASK = "repair_task"
    PLAN_REVISION = "plan_revision"


class TaskGraphMutation(BaseModel):
    mutation_id: str = Field(default_factory=lambda: "mut_" + uuid.uuid4().hex[:16])
    run_id: str
    mutation_type: TaskGraphMutationType
    actor_agent_id: str
    payload: dict[str, Any]
    expected_version: int | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TaskGraphVersion(BaseModel):
    run_id: str
    version: int
    mutation_id: str
    graph: TaskGraph


def _ensure_schema() -> None:
    conn = _get_conn()
    from app.infrastructure.database.run_store import (
        _ensure_task_board_tasks, _ensure_task_graph_snapshots,
    )
    _ensure_task_board_tasks(conn)
    _ensure_task_graph_snapshots(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_graph_mutations (
            mutation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            actor_agent_id TEXT NOT NULL,
            mutation_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            result_version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_graph_mutations_run
            ON task_graph_mutations(run_id, result_version);
        CREATE TABLE IF NOT EXISTS control_plane_outbox (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        );
        """
    )
    conn.commit()


class TransactionalTaskService:
    """TaskGraph is plan structure; TaskBoard remains runtime authority."""

    def __init__(self) -> None:
        _ensure_schema()

    def graph(self, run_id: str) -> TaskGraph:
        payload = get_agent_run_history().load_task_graph(run_id)
        if payload is None:
            raise KeyError(f"task graph not found for run {run_id}")
        return TaskGraph.model_validate(payload)

    def register_initial_graph(self, run_id: str, graph: TaskGraph,
                               actor_agent_id: str = "orchestrator") -> TaskGraphVersion:
        """Atomically materialize a planner graph into the runtime TaskBoard.

        This is the only plan-to-runtime write boundary.  Repeated calls use
        stable mutation ids and never overwrite existing Board runtime state.
        """
        graph.validate()
        already_registered = _get_conn().execute(
            "SELECT 1 FROM task_graph_mutations WHERE run_id=? AND mutation_type='initial_task' LIMIT 1",
            (run_id,),
        ).fetchone()
        if already_registered:
            current = self.graph(run_id)
            if self._plan_signature(current) == self._plan_signature(graph):
                get_task_board().restore_run(run_id)
                return TaskGraphVersion(
                    run_id=run_id,
                    version=current.version,
                    mutation_id=f"initial:{run_id}",
                    graph=current,
                )
            return self._replace_plan(run_id, graph, actor_agent_id)
        board_tasks: list[BoardTask] = []
        for node in graph.nodes.values():
            hook = get_lifecycle_hook_engine().emit(
                LifecycleEvent.TASK_CREATED,
                {"run_id": run_id, "agent_id": actor_agent_id, "task_id": node.id,
                 "task": node.model_dump(mode="json")},
            )
            if hook.block or not hook.allow:
                raise PermissionError(hook.feedback or "TaskCreated hook blocked initial plan")
            node.metadata.update(hook.mutate_metadata)
            board_tasks.append(BoardTask(
                task_id=node.id, run_id=run_id, title=node.title or node.id,
                objective=node.objective, dependencies=list(node.dependencies),
                required_capabilities=list(node.required_capabilities),
                priority=node.priority, max_attempts=node.max_attempts,
                metadata={"graph_version": graph.version},
            ))

        conn = _get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM task_graph_mutations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if int(existing["n"] or 0) == 0:
                conn.execute(
                    "INSERT INTO task_graph_snapshots(run_id, version, graph_json, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                    "version=excluded.version, graph_json=excluded.graph_json, updated_at=excluded.updated_at",
                    (run_id, graph.version, json.dumps(graph.model_dump(mode="json")),
                     datetime.utcnow().isoformat()),
                )
                sequence = int(conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS seq FROM control_plane_outbox WHERE run_id=?",
                    (run_id,),
                ).fetchone()["seq"])
                for task in board_tasks:
                    mutation_id = f"initial:{run_id}:{task.task_id}"
                    conn.execute(
                        "INSERT OR IGNORE INTO task_board_tasks(run_id, task_id, payload, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (run_id, task.task_id, json.dumps(task.model_dump(mode="json")),
                         datetime.utcnow().isoformat()),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO task_graph_mutations VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (mutation_id, run_id, actor_agent_id, "initial_task",
                         json.dumps({"task_id": task.task_id}), graph.version,
                         datetime.utcnow().isoformat()),
                    )
                    sequence += 1
                    conn.execute(
                        "INSERT OR IGNORE INTO control_plane_outbox VALUES (?, ?, ?, ?, ?, ?)",
                        ("evt_" + uuid.uuid4().hex[:16], run_id, "TaskCreated", sequence,
                         json.dumps({"mutation_id": mutation_id,
                                     "task_id": task.task_id,
                                     "version": graph.version}),
                         datetime.utcnow().isoformat()),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        get_task_board().restore_run(run_id)
        return TaskGraphVersion(run_id=run_id, version=graph.version,
                                mutation_id=f"initial:{run_id}", graph=graph)

    @staticmethod
    def _plan_signature(graph: TaskGraph) -> str:
        """Compare plan structure without runtime status or graph counters."""
        nodes = {}
        for node_id, node in sorted(graph.nodes.items()):
            nodes[node_id] = {
                "title": node.title,
                "objective": node.objective,
                "description": node.description,
                "dependencies": sorted(node.dependencies),
                "required_capabilities": sorted(node.required_capabilities),
                "preferred_agent_profile": node.preferred_agent_profile,
                "output_contract": node.output_contract.model_dump(mode="json"),
                "priority": node.priority,
                "max_attempts": node.max_attempts,
                "budget": node.budget.model_dump(mode="json"),
            }
        return json.dumps(
            {"root_task_id": graph.root_task_id, "nodes": nodes},
            ensure_ascii=False,
            sort_keys=True,
        )

    def _replace_plan(
        self,
        run_id: str,
        incoming: TaskGraph,
        actor_agent_id: str,
    ) -> TaskGraphVersion:
        """Atomically supersede the old Board projection with a new plan."""
        incoming = incoming.model_copy(deep=True)
        incoming.validate()
        board = get_task_board()
        old_tasks = board.list_by_run(run_id)
        occupied_ids = {task.task_id for task in old_tasks}
        current = self.graph(run_id)
        revision = max(current.version + 1, incoming.version)
        id_map = {
            node_id: (
                f"plan_v{revision}__{node_id}"
                if node_id in occupied_ids else node_id
            )
            for node_id in incoming.nodes
        }
        revised_nodes: dict[str, TaskNode] = {}
        from app.multiagent.task_graph import TaskNodeStatus
        for old_id, source in incoming.nodes.items():
            node = source.model_copy(deep=True)
            node.id = id_map[old_id]
            node.dependencies = [id_map[dependency] for dependency in node.dependencies]
            node.status = TaskNodeStatus.PENDING
            node.assigned_agent_id = None
            node.input_artifact_ids = []
            node.output_artifact_ids = []
            node.attempts = 0
            node.error = None
            node.started_at = None
            node.completed_at = None
            node.metadata = {
                **node.metadata,
                "plan_revision": revision,
                "original_task_id": old_id,
            }
            hook = get_lifecycle_hook_engine().emit(
                LifecycleEvent.TASK_CREATED,
                {
                    "run_id": run_id,
                    "agent_id": actor_agent_id,
                    "task_id": node.id,
                    "task": node.model_dump(mode="json"),
                    "plan_revision": revision,
                },
            )
            if hook.block or not hook.allow:
                raise PermissionError(
                    hook.feedback or "TaskCreated hook blocked revised plan"
                )
            node.metadata.update(hook.mutate_metadata)
            revised_nodes[node.id] = node
        revised = TaskGraph(
            root_task_id=id_map[incoming.root_task_id],
            nodes=revised_nodes,
            version=revision,
        )
        revised.validate()
        new_board_tasks = [
            BoardTask(
                task_id=node.id,
                run_id=run_id,
                title=node.title or node.id,
                objective=node.objective,
                dependencies=list(node.dependencies),
                required_capabilities=list(node.required_capabilities),
                priority=node.priority,
                max_attempts=node.max_attempts,
                metadata={
                    "graph_version": revised.version,
                    "plan_revision": revision,
                },
            )
            for node in revised.nodes.values()
        ]
        mutation_id = f"plan-revision:{run_id}:{revision}"
        superseded_tasks: list[BoardTask] = []
        conn = _get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for task in old_tasks:
                updated = task.model_copy(deep=True)
                updated.metadata["superseded_by_plan_revision"] = revision
                updated.updated_at = datetime.utcnow()
                superseded_tasks.append(updated)
                conn.execute(
                    "UPDATE task_board_tasks SET payload=?, updated_at=? "
                    "WHERE run_id=? AND task_id=?",
                    (
                        json.dumps(updated.model_dump(mode="json")),
                        updated.updated_at.isoformat(),
                        run_id,
                        updated.task_id,
                    ),
                )
            for task in new_board_tasks:
                conn.execute(
                    "INSERT INTO task_board_tasks(run_id, task_id, payload, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        run_id,
                        task.task_id,
                        json.dumps(task.model_dump(mode="json")),
                        datetime.utcnow().isoformat(),
                    ),
                )
            conn.execute(
                "INSERT INTO task_graph_snapshots(run_id, version, graph_json, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "version=excluded.version, graph_json=excluded.graph_json, "
                "updated_at=excluded.updated_at",
                (
                    run_id,
                    revised.version,
                    json.dumps(revised.model_dump(mode="json")),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO task_graph_mutations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    mutation_id,
                    run_id,
                    actor_agent_id,
                    TaskGraphMutationType.PLAN_REVISION.value,
                    json.dumps({
                        "previous_version": current.version,
                        "task_ids": list(revised.nodes),
                    }),
                    revised.version,
                    datetime.utcnow().isoformat(),
                ),
            )
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq "
                "FROM control_plane_outbox WHERE run_id=?",
                (run_id,),
            ).fetchone()["seq"])
            conn.execute(
                "INSERT INTO control_plane_outbox VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "evt_" + uuid.uuid4().hex[:16],
                    run_id,
                    "TaskGraphReplanned",
                    sequence,
                    json.dumps({
                        "mutation_id": mutation_id,
                        "previous_version": current.version,
                        "version": revised.version,
                        "task_ids": list(revised.nodes),
                    }),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        for task in [*superseded_tasks, *new_board_tasks]:
            board.add(task)
        return TaskGraphVersion(
            run_id=run_id,
            version=revised.version,
            mutation_id=mutation_id,
            graph=revised,
        )

    def apply(self, mutation: TaskGraphMutation) -> TaskGraphVersion:
        _ensure_schema()
        existing = _get_conn().execute(
            "SELECT result_version FROM task_graph_mutations WHERE mutation_id=?",
            (mutation.mutation_id,),
        ).fetchone()
        if existing:
            graph = self.graph(mutation.run_id)
            return TaskGraphVersion(run_id=mutation.run_id,
                                    version=int(existing["result_version"]),
                                    mutation_id=mutation.mutation_id, graph=graph)
        graph = self.graph(mutation.run_id)
        if mutation.expected_version is not None and graph.version != mutation.expected_version:
            raise RuntimeError(f"task graph version conflict: expected {mutation.expected_version}, got {graph.version}")

        board_task: BoardTask | None = None
        board_updates: list[BoardTask] = []
        if mutation.mutation_type == TaskGraphMutationType.CREATE_TASK:
            node = TaskNode.model_validate(mutation.payload["task"])
            hook = get_lifecycle_hook_engine().emit(
                LifecycleEvent.TASK_CREATED,
                {"run_id": mutation.run_id, "agent_id": mutation.actor_agent_id,
                 "task_id": node.id, "task": node.model_dump(mode="json")},
            )
            if hook.block or not hook.allow:
                raise PermissionError(hook.feedback or "TaskCreated hook blocked mutation")
            node.metadata.update(hook.mutate_metadata)
            graph.add_node(node)
            graph.validate()
            board_task = BoardTask(
                task_id=node.id, run_id=mutation.run_id, title=node.title or node.id,
                objective=node.objective, dependencies=list(node.dependencies),
                required_capabilities=list(node.required_capabilities),
                priority=node.priority, max_attempts=node.max_attempts,
                metadata={"graph_version": graph.version},
            )
        elif mutation.mutation_type == TaskGraphMutationType.ADD_DEPENDENCY:
            task_id = mutation.payload["task_id"]
            dependency_id = mutation.payload["dependency_id"]
            if task_id not in graph.nodes or dependency_id not in graph.nodes:
                raise ValueError("dependency references unknown task")
            node = graph.nodes[task_id]
            if dependency_id not in node.dependencies:
                node.dependencies.append(dependency_id)
                graph._touch()
            graph.validate()
        elif mutation.mutation_type == TaskGraphMutationType.UPDATE_TASK:
            task_id = mutation.payload["task_id"]
            if task_id not in graph.nodes:
                raise KeyError(task_id)
            allowed = {"title", "objective", "description", "priority", "metadata"}
            for key, value in mutation.payload.get("changes", {}).items():
                if key not in allowed:
                    raise ValueError(f"field cannot be mutated at runtime: {key}")
                if key == "metadata":
                    graph.nodes[task_id].metadata.update(value)
                else:
                    setattr(graph.nodes[task_id], key, value)
            graph._touch()
        elif mutation.mutation_type == TaskGraphMutationType.REPAIR_TASK:
            from app.multiagent.task_graph import TaskNodeStatus
            target_id = mutation.payload["target_task_id"]
            if target_id not in graph.nodes:
                raise KeyError(target_id)
            target = graph.nodes[target_id]
            if target.status == TaskNodeStatus.RUNNING:
                graph.update_status(target_id, TaskNodeStatus.FAILED)
            repair = graph.add_repair_task(
                target_id,
                mutation.payload.get("objective") or f"repair {target.objective}",
                required_capabilities=(mutation.payload.get("required_capabilities")
                                       or list(target.required_capabilities)),
            )
            repair.metadata.update({
                "repair_of": target_id,
                "source_artifact_ids": list(mutation.payload.get("source_artifact_ids", [])),
                "verification_feedback": mutation.payload.get("verification_feedback", {}),
            })
            board_task = BoardTask(
                task_id=repair.id, run_id=mutation.run_id,
                title=repair.title, objective=repair.objective,
                dependencies=list(repair.dependencies),
                required_capabilities=list(repair.required_capabilities),
                priority=repair.priority, max_attempts=repair.max_attempts,
                metadata={"graph_version": graph.version, **repair.metadata},
            )
            board = get_task_board()
            target_board = board.get(target_id, run_id=mutation.run_id)
            if target_board is not None:
                target_board = target_board.model_copy(deep=True)
                target_board.metadata["superseded_by_repair"] = repair.id
                target_board.updated_at = datetime.utcnow()
                board_updates.append(target_board)
            for node in graph.nodes.values():
                if node.id == repair.id:
                    continue
                runtime_task = board.get(node.id, run_id=mutation.run_id)
                if runtime_task is not None and runtime_task.dependencies != node.dependencies:
                    runtime_task = runtime_task.model_copy(deep=True)
                    runtime_task.dependencies = list(node.dependencies)
                    runtime_task.updated_at = datetime.utcnow()
                    board_updates.append(runtime_task)
        else:  # pragma: no cover - exhaustive enum guard
            raise ValueError(mutation.mutation_type)

        conn = _get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT version FROM task_graph_snapshots WHERE run_id=?", (mutation.run_id,)
            ).fetchone()
            if current and mutation.expected_version is not None and int(current["version"]) != mutation.expected_version:
                raise RuntimeError("task graph changed while applying mutation")
            conn.execute(
                "INSERT INTO task_graph_snapshots(run_id, version, graph_json, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "version=excluded.version, graph_json=excluded.graph_json, updated_at=excluded.updated_at",
                (mutation.run_id, graph.version, json.dumps(graph.model_dump(mode="json")),
                 datetime.utcnow().isoformat()),
            )
            if board_task is not None:
                conn.execute(
                    "INSERT INTO task_board_tasks(run_id, task_id, payload, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(run_id, task_id) DO NOTHING",
                    (board_task.run_id, board_task.task_id,
                     json.dumps(board_task.model_dump(mode="json")), datetime.utcnow().isoformat()),
                )
            for updated_task in board_updates:
                conn.execute(
                    "UPDATE task_board_tasks SET payload=?, updated_at=? "
                    "WHERE run_id=? AND task_id=?",
                    (json.dumps(updated_task.model_dump(mode="json")),
                     datetime.utcnow().isoformat(), updated_task.run_id,
                     updated_task.task_id),
                )
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM control_plane_outbox WHERE run_id=?",
                (mutation.run_id,),
            ).fetchone()["seq"])
            conn.execute(
                "INSERT INTO task_graph_mutations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mutation.mutation_id, mutation.run_id, mutation.actor_agent_id,
                 mutation.mutation_type.value, json.dumps(mutation.payload), graph.version,
                 mutation.created_at.isoformat()),
            )
            conn.execute(
                "INSERT INTO control_plane_outbox VALUES (?, ?, ?, ?, ?, ?)",
                ("evt_" + uuid.uuid4().hex[:16], mutation.run_id, "TaskGraphMutation",
                 sequence, json.dumps({"mutation_id": mutation.mutation_id,
                                       "version": graph.version}),
                 datetime.utcnow().isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if board_task is not None:
            board = get_task_board()
            if board.get(board_task.task_id, run_id=mutation.run_id) is None:
                # Add to the in-process projection after the transaction.  Its
                # durable row already exists and add() is an idempotent upsert.
                board.add(board_task)
            for updated_task in board_updates:
                board.add(updated_task)
        elif mutation.mutation_type == TaskGraphMutationType.ADD_DEPENDENCY:
            board = get_task_board()
            task = board.get(mutation.payload["task_id"], run_id=mutation.run_id)
            if task is not None:
                task.dependencies = list(graph.nodes[task.task_id].dependencies)
                board.add(task)
        return TaskGraphVersion(run_id=mutation.run_id, version=graph.version,
                                mutation_id=mutation.mutation_id, graph=graph)

    def create_task(self, run_id: str, actor_agent_id: str, task: dict[str, Any],
                    mutation_id: str | None = None) -> TaskGraphVersion:
        return self.apply(TaskGraphMutation(
            mutation_id=mutation_id or "mut_" + uuid.uuid4().hex[:16],
            run_id=run_id, mutation_type=TaskGraphMutationType.CREATE_TASK,
            actor_agent_id=actor_agent_id, payload={"task": task},
        ))

    def add_dependency(self, run_id: str, actor_agent_id: str, task_id: str,
                       dependency_id: str, mutation_id: str | None = None) -> TaskGraphVersion:
        return self.apply(TaskGraphMutation(
            mutation_id=mutation_id or "mut_" + uuid.uuid4().hex[:16],
            run_id=run_id, mutation_type=TaskGraphMutationType.ADD_DEPENDENCY,
            actor_agent_id=actor_agent_id,
            payload={"task_id": task_id, "dependency_id": dependency_id},
        ))

    def create_repair(self, run_id: str, target_task_id: str, *,
                      objective: str, required_capabilities: list[str],
                      source_artifact_ids: list[str],
                      verification_feedback: dict[str, Any],
                      mutation_id: str | None = None) -> TaskGraphVersion:
        return self.apply(TaskGraphMutation(
            mutation_id=mutation_id or "mut_" + uuid.uuid4().hex[:16],
            run_id=run_id, mutation_type=TaskGraphMutationType.REPAIR_TASK,
            actor_agent_id="verifier",
            payload={"target_task_id": target_task_id, "objective": objective,
                     "required_capabilities": required_capabilities,
                     "source_artifact_ids": source_artifact_ids,
                     "verification_feedback": verification_feedback},
        ))
