"""ResumeCoordinator — 基于 checkpoint 的运行恢复协调器。

Phase G 第 1 步：恢复（docs/MegaDeepagents_Agent_Teams_改造任务书.md §16）：
- 跨进程重启后，从 SqliteSaver checkpoint 恢复各 Agent 状态
- 已完成 Task 不重新执行（通过 task_runs.resumed_checkpoints 跳过）
- 持续 Teammate 不重新执行 → 直接进入主链调度下一个 ready task
- 失败 Task 视为可重新调度（重试）

设计：
- 协调器读 task_runs.get_resumed_checkpoints(run_id)
- 用 checkpoint_id + SqliteSaver 调用 graph.aget_state(checkpoint_id)
- 把状态注入 AgentRegistry（AgentInstance 重新构造，session_id 保留 = same Agent）
- 把 task 状态同步到 TaskBoard（已 succeeded 的清零再标 SUCCEEDED）

兼容性：
- 没有 SqliteSaver 时回退到 dummy（直接刷新 TaskBoard + AgentRegistry）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_instance import AgentInstance, AgentStatus
from app.multiagent.agent_registry import get_agent_registry
from app.multiagent.phase_g_store import AgentRunHistory, get_agent_run_history
from app.multiagent.task_board import (
    BoardTask,
    BoardTaskStatus,
    TaskBoard,
    get_task_board,
)


class ResumeResult:
    """恢复结果统计。"""
    def __init__(self) -> None:
        self.resumed_agents: int = 0
        self.skipped_tasks: int = 0
        self.pending_agents_recreated: int = 0
        self.errors: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "resumed_agents": self.resumed_agents,
            "skipped_tasks": self.skipped_tasks,
            "pending_agents_recreated": self.pending_agents_recreated,
            "errors": self.errors,
        }


class ResumeCoordinator:
    """协调恢复流程：load → reconstruct → 跳过已 succeeded task。"""

    def __init__(
        self,
        board: TaskBoard | None = None,
        registry: Any | None = None,
        history: AgentRunHistory | None = None,
        checkpoint_loader: Any | None = None,
    ) -> None:
        self.board = board or get_task_board()
        self.registry = registry or get_agent_registry()
        self.history = history or get_agent_run_history()
        # checkpoint_loader: 可选的 callable(run_id, agent_id) -> checkpoint_dict or None
        self.checkpoint_loader = checkpoint_loader or _default_checkpoint_loader

    def resume(self, run_id: str) -> ResumeResult:
        """执行恢复流程。"""
        result = ResumeResult()
        logger.info(f"[Resume] start for run={run_id}")

        # TaskBoard is the durable data plane.  Rehydrate it before restoring
        # agents so a restarted scheduler sees the same work, not an empty run.
        self.board.restore_run(run_id)
        self.board.prepare_for_resume(run_id)

        # A user/peer message is work input.  It must be available before the
        # scheduler reserves a restored teammate; otherwise a restart loses a
        # wake-up that was already durably delivered.
        from app.multiagent.mailbox import get_mailbox
        get_mailbox().restore_from_db(run_id, history=self.history)
        from app.multiagent.tool_runtime import ToolSideEffectJournal
        incomplete_tools = ToolSideEffectJournal().recover_incomplete(run_id)
        for invocation in incomplete_tools:
            result.errors.append(
                f"tool {invocation.invocation_id}: {invocation.status.value}"
            )

        # 1. 从持久化读 Agent 列表
        persisted_agents = self.history.list_by_run(run_id)

        # 2. 为每个 Agent 重建 AgentInstance + 注入 registry
        for stored in persisted_agents:
            agent_id = stored["agent_id"]
            try:
                # 载入 checkpoint（如果有 checkpoint_id）
                ckpt = self.checkpoint_loader(run_id, agent_id)
                if ckpt is not None:
                    logger.info(
                        f"[Resume] loaded checkpoint for agent={agent_id} "
                        f"session_id={stored.get('session_id')}"
                    )
                # 注意：AgentInstance Registry 是 in-memory。Phase G 的恢复机制把它
                # 重新加入 registry 时，可能与其他已注册 Agent 冲突。我们只重建
                # 仍存活的（status not stopped/failed）的 Agent。
                if stored.get("status") in ("stopped", "failed"):
                    logger.info(
                        f"[Resume] skip {agent_id} (status={stored['status']})"
                    )
                    continue
                # 调用 registry.create_agent 重建
                capabilities = stored.get("capabilities") or []
                if isinstance(capabilities, str):
                    import json
                    capabilities = json.loads(capabilities)
                agent = self.registry.create_agent(
                    profile_id=stored.get("profile_id", "default"),
                    name=stored.get("name", ""),
                    role=stored.get("role", ""),
                    team_id=stored.get("team_id", ""),
                    run_id=run_id,
                    capabilities=capabilities,
                    agent_id_override=agent_id,
                    session_id_override=stored.get("session_id"),
                    thread_id_override=stored.get("thread_id"),
                    checkpoint_namespace_override=stored.get("checkpoint_namespace"),
                    workspace_root=stored.get("workspace_root") or "",
                    metadata=stored.get("metadata") or {},
                    worktree_path=(stored.get("metadata") or {}).get("worktree_path", ""),
                    mailbox_cursor=int((stored.get("metadata") or {}).get("mailbox_cursor", 0)),
                )
                # A process cannot safely resume a RUNNING/CLAIMING lease.
                # It is requeued above, therefore the teammate comes back IDLE
                # with the same identity/session/checkpoint namespace.
                target_status = AgentStatus(stored.get("status", "idle"))
                if target_status in (AgentStatus.RUNNING, AgentStatus.CLAIMING):
                    target_status = AgentStatus.IDLE
                if target_status not in (AgentStatus.CREATED, AgentStatus.IDLE):
                    try:
                        agent.update_status(target_status)
                    except Exception:
                        # 不合法转换，留为 idle
                        pass
                result.resumed_agents += 1
                from app.multiagent.teammate_session import (
                    TeammateLifecycle, get_teammate_supervisor,
                )
                supervisor = get_teammate_supervisor()
                session = supervisor.ensure_session(agent)
                if session.lifecycle_state in (
                    TeammateLifecycle.CLAIMING, TeammateLifecycle.PLANNING,
                    TeammateLifecycle.RUNNING, TeammateLifecycle.WAITING_TOOL,
                ):
                    session.transition(TeammateLifecycle.IDLE)
                    session.current_task_id = None
                    supervisor.persist(session)
            except Exception as exc:
                logger.error(f"[Resume] failed to reconstruct agent={agent_id}: {exc}")
                result.errors.append(f"agent {agent_id}: {exc}")

        # 3. TaskBoard itself is the runtime authority.  A historical
        # task_runs row can never promote PENDING to SUCCEEDED: it may describe
        # worker output that was never verified.  Already-SUCCEEDED Board rows
        # were preserved by prepare_for_resume and are simply counted here.
        result.skipped_tasks = sum(
            1 for task in self.board.list_by_run(run_id)
            if task.status == BoardTaskStatus.SUCCEEDED
        )

        # Approved permission requests unblock their exact task on resume.
        from app.multiagent.permission import get_permission_broker
        broker = get_permission_broker()
        for task in self.board.list_by_run(run_id):
            if task.status != BoardTaskStatus.BLOCKED:
                continue
            request_id = task.metadata.get("permission_request_id")
            if request_id:
                request = broker.get(request_id)
                if request and request.status == "approved":
                    task.status = BoardTaskStatus.PENDING
                    task.claimed_by = None
                    task.claimed_at = None
                    self.board.add(task)
                    continue
            plan_id = task.metadata.get("plan_id")
            if plan_id:
                from app.multiagent.plan_approval import PlanApprovalService, PlanStatus
                plan = PlanApprovalService().get(plan_id)
                if plan and plan.status == PlanStatus.PLAN_APPROVED:
                    task.status = BoardTaskStatus.PENDING
                    task.claimed_by = None
                    task.claimed_at = None
                    self.board.add(task)

        # 4. 留下未完成的（PENDING / RUNNING）task 由主链继续调度
        logger.info(
            f"[Resume] done for run={run_id}: resumed={result.resumed_agents} "
            f"skipped_tasks={result.skipped_tasks} errors={len(result.errors)}"
        )
        return result


def _default_checkpoint_loader(run_id: str, agent_id: str) -> dict[str, Any] | None:
    """默认 checkpoint loader：从 SqliteSaver 真实加载该 Agent 的最新 checkpoint 状态。

    策略：
    1. 先从 `agent_instances` 持久化记录读出该 agent 的 `thread_id` 与 `checkpoint_namespace`
       （在 deepagents graph 中，thread_id 唯一标识一个 Agent 的会话线）；
    2. 用 `_get_sqlite_saver().aget(config)` 同步等价地拉取该 thread 的最新 StateSnapshot；
    3. 解析出 `checkpoint_id`、`next_step` 等，返回 dict 供后续注入。

    说明：deepagents/langgraph 的 SqliteSaver 是异步接口（aget_state），
    本函数在 ResumeCoordinator 的同步路径中调用，故使用 asyncio.run 包一层。
    在生产部署中，调用方应在 async 上下文里调用 `aload_checkpoint`。
    """
    history = get_agent_run_history()
    stored = history.get_agent_instance(agent_id)
    if not stored:
        return None
    thread_id = stored.get("thread_id")
    if not thread_id:
        return None
    return _load_checkpoint_sync(thread_id, agent_id, run_id)


async def aload_checkpoint(run_id: str, agent_id: str) -> dict[str, Any] | None:
    """异步版本 checkpoint loader，给 async 调用路径用。"""
    history = get_agent_run_history()
    stored = history.get_agent_instance(agent_id)
    if not stored:
        return None
    thread_id = stored.get("thread_id")
    if not thread_id:
        return None
    return await _load_checkpoint_async(thread_id, agent_id, run_id)


def _load_checkpoint_sync(thread_id: str, agent_id: str, run_id: str) -> dict[str, Any] | None:
    try:
        from app.core.agent_factory import _get_sqlite_saver
        saver = _get_sqlite_saver()
    except Exception as exc:
        logger.warning(f"[Resume] SqliteSaver 不可用，跳过 checkpoint 加载: {exc}")
        return None
    config = {"configurable": {"thread_id": thread_id}}
    try:
        import asyncio
        snapshot = asyncio.run(saver.aget(config))
    except RuntimeError:
        # 嵌套 event loop 场景：用同步接口（langgraph 通常提供 get() 兼容）
        try:
            snapshot = saver.get(config)
        except Exception as exc:
            logger.warning(f"[Resume] saver.get 失败 thread={thread_id}: {exc}")
            return None
    except Exception as exc:
        logger.warning(f"[Resume] saver.aget 失败 thread={thread_id}: {exc}")
        return None
    if snapshot is None:
        return None
    return _snapshot_to_dict(snapshot, thread_id, agent_id, run_id)


async def _load_checkpoint_async(thread_id: str, agent_id: str, run_id: str) -> dict[str, Any] | None:
    try:
        from app.core.agent_factory import _get_sqlite_saver
        saver = _get_sqlite_saver()
    except Exception as exc:
        logger.warning(f"[Resume] SqliteSaver 不可用: {exc}")
        return None
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await saver.aget(config)
    except Exception as exc:
        logger.warning(f"[Resume] saver.aget 失败 thread={thread_id}: {exc}")
        return None
    if snapshot is None:
        return None
    return _snapshot_to_dict(snapshot, thread_id, agent_id, run_id)


def _snapshot_to_dict(snapshot: Any, thread_id: str, agent_id: str, run_id: str) -> dict[str, Any]:
    """把 LangGraph StateSnapshot 提取为可注入 dict。"""
    result: dict[str, Any] = {
        "thread_id": thread_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "checkpoint_id": None,
        "next_step": None,
        "metadata": {},
    }
    try:
        # StateSnapshot has .next, .config, .metadata, .values
        result["checkpoint_id"] = (
            getattr(snapshot, "config", {}).get("configurable", {}).get("checkpoint_id")
            if isinstance(getattr(snapshot, "config", None), dict)
            else None
        )
        nxt = getattr(snapshot, "next", None)
        result["next_step"] = (nxt[0] if nxt and len(nxt) > 0 else None) if nxt else None
        result["metadata"] = dict(getattr(snapshot, "metadata", {}) or {})
        try:
            result["values"] = dict(getattr(snapshot, "values", {}) or {})
        except Exception:
            result["values"] = {}
    except Exception as exc:
        logger.warning(f"[Resume] snapshot 解析失败: {exc}")
    return result


# ===== 全局 =====

_coordinator: ResumeCoordinator | None = None


def get_resume_coordinator() -> ResumeCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = ResumeCoordinator()
    return _coordinator


def reset_resume_coordinator() -> None:
    global _coordinator
    _coordinator = None
