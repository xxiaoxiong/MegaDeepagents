"""Authoritative, audited team collaboration tools exposed to teammates."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from app.multiagent.agent_registry import get_agent_registry
from app.multiagent.dynamic_team import DynamicTeamManager
from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
from app.multiagent.mailbox import MailboxMessage, get_mailbox, make_message_id
from app.multiagent.permission import PermissionKind, get_permission_broker
from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id
from app.multiagent.plan_approval import PlanApprovalService, TeammatePlan
from app.multiagent.task_board import BoardTaskStatus, get_task_board
from app.multiagent.task_graph import TaskNode
from app.multiagent.teammate_session import TeammateCommandType, get_teammate_supervisor
from app.multiagent.transactional_task_service import TransactionalTaskService


class TeamControlPlaneService:
    """Only this service may implement an Agent-requested team mutation."""

    def __init__(self) -> None:
        self.registry = get_agent_registry()
        self.board = get_task_board()
        self.mailbox = get_mailbox()
        self.tasks = TransactionalTaskService()
        self.permissions = get_permission_broker()
        self.dynamic_team = DynamicTeamManager(self.registry)
        self.plan_approvals = PlanApprovalService()
        self.lead_agent_id = "lead"

    def _caller(self, run_id: str, agent_id: str) -> Any:
        if agent_id == self.lead_agent_id:
            return type("Lead", (), {"agent_id": "lead", "run_id": run_id,
                                      "team_id": "", "status": "running"})()
        agent = self.registry.get(agent_id)
        if agent is None or agent.run_id != run_id:
            raise PermissionError("caller is not a member of this run")
        return agent

    def _audit(self, name: str, run_id: str, agent_id: str,
               payload: dict[str, Any] | None = None) -> None:
        get_agent_run_history().record_event(
            event_id=make_run_event_id(), run_id=run_id,
            event_type=f"team_tool:{name}", agent_id=agent_id, payload=payload or {},
        )

    def team_list_members(self, run_id: str, agent_id: str) -> list[dict[str, Any]]:
        self._caller(run_id, agent_id)
        result = [{"agent_id": a.agent_id, "name": a.name, "role": a.role,
                   "status": getattr(a.status, "value", a.status),
                   "current_task_id": a.current_task_id, "session_id": a.session_id}
                  for a in self.registry.list_by_run(run_id)]
        self._audit("list_members", run_id, agent_id, {"count": len(result)})
        return result

    def team_get_member_status(self, run_id: str, agent_id: str,
                               member_agent_id: str) -> dict[str, Any]:
        self._caller(run_id, agent_id)
        target = self.registry.get(member_agent_id)
        if target is None or target.run_id != run_id:
            raise KeyError(member_agent_id)
        return self.team_list_members(run_id, agent_id)[
            [a.agent_id for a in self.registry.list_by_run(run_id)].index(member_agent_id)
        ]

    def team_list_tasks(self, run_id: str, agent_id: str) -> list[dict[str, Any]]:
        self._caller(run_id, agent_id)
        result = [task.model_dump(mode="json") for task in self.board.list_by_run(run_id)]
        self._audit("list_tasks", run_id, agent_id, {"count": len(result)})
        return result

    def team_get_task(self, run_id: str, agent_id: str, task_id: str) -> dict[str, Any]:
        self._caller(run_id, agent_id)
        task = self.board.get(task_id, run_id=run_id)
        if task is None:
            raise KeyError(task_id)
        self._audit("get_task", run_id, agent_id, {"task_id": task_id})
        return task.model_dump(mode="json")

    def team_claim_task(self, run_id: str, agent_id: str, task_id: str) -> dict[str, Any]:
        agent = self._caller(run_id, agent_id)
        task = self.board.get(task_id, run_id=run_id)
        if task is None:
            raise KeyError(task_id)
        if not set(task.required_capabilities).issubset(set(agent.capabilities)):
            raise PermissionError("agent lacks task capabilities")
        result = self.board.claim(task_id, agent_id, run_id=run_id)
        self._audit("claim_task", run_id, agent_id,
                    {"task_id": task_id, "success": result.success, "reason": result.reason})
        return result.model_dump(mode="json")

    def team_create_task(self, run_id: str, agent_id: str, task: dict[str, Any],
                         mutation_id: str | None = None) -> dict[str, Any]:
        self._caller(run_id, agent_id)
        self.permissions.authorize(run_id=run_id, agent_id=agent_id,
                                   kind=PermissionKind.TASK_CREATE,
                                   operation="team_create_task",
                                   parameters={"task_id": task.get("id")})
        version = self.tasks.create_task(run_id, agent_id, task, mutation_id)
        self._audit("create_task", run_id, agent_id,
                    {"task_id": task.get("id"), "version": version.version})
        return {"mutation_id": version.mutation_id, "version": version.version,
                "task_id": task.get("id")}

    def team_update_task(self, run_id: str, agent_id: str, task_id: str,
                         changes: dict[str, Any], mutation_id: str | None = None) -> dict[str, Any]:
        self._caller(run_id, agent_id)
        # Status and success are verifier/control-plane owned.
        if "status" in changes or "claimed_by" in changes:
            raise PermissionError("agent cannot directly change task runtime state")
        from app.multiagent.transactional_task_service import TaskGraphMutation, TaskGraphMutationType
        mutation = TaskGraphMutation(run_id=run_id, actor_agent_id=agent_id,
                                     mutation_type=TaskGraphMutationType.UPDATE_TASK,
                                     payload={"task_id": task_id, "changes": changes})
        if mutation_id:
            mutation.mutation_id = mutation_id
        result = self.tasks.apply(mutation)
        self._audit("update_task", run_id, agent_id, {"task_id": task_id})
        return {"version": result.version, "mutation_id": result.mutation_id}

    def team_add_dependency(self, run_id: str, agent_id: str, task_id: str,
                            dependency_id: str, mutation_id: str | None = None) -> dict[str, Any]:
        self._caller(run_id, agent_id)
        result = self.tasks.add_dependency(run_id, agent_id, task_id, dependency_id, mutation_id)
        self._audit("add_dependency", run_id, agent_id,
                    {"task_id": task_id, "dependency_id": dependency_id})
        return {"version": result.version, "mutation_id": result.mutation_id}

    def team_mark_blocked(self, run_id: str, agent_id: str, task_id: str,
                          reason: str) -> bool:
        self._caller(run_id, agent_id)
        task = self.board.get(task_id, run_id=run_id)
        if task is None or task.claimed_by != agent_id or task.status != BoardTaskStatus.RUNNING:
            return False
        task.status = BoardTaskStatus.BLOCKED
        task.last_error = reason
        self.board.add(task)
        self._audit("mark_blocked", run_id, agent_id, {"task_id": task_id, "reason": reason})
        return True

    def team_request_replan(self, run_id: str, agent_id: str, reason: str) -> bool:
        self._caller(run_id, agent_id)
        self._audit("request_replan", run_id, agent_id, {"reason": reason})
        return True

    def team_send_message(self, run_id: str, agent_id: str, to_agent_id: str,
                          content: str, title: str = "team_message") -> bool:
        sender = self._caller(run_id, agent_id)
        target = self.registry.get(to_agent_id)
        if target is None or target.run_id != run_id:
            raise KeyError(to_agent_id)
        message = MailboxMessage(message_id=make_message_id(), from_agent_id=agent_id,
                                 from_agent_name=getattr(sender, "name", "lead"),
                                 to_agent_id=to_agent_id, run_id=run_id,
                                 title=title, content=content)
        ok = self.mailbox.send(message)
        if ok:
            session = get_teammate_supervisor().load(to_agent_id)
            if session:
                from app.multiagent.teammate_session import TeammateCommandQueue
                TeammateCommandQueue(session.session_id).put(
                    TeammateCommandType.MESSAGE.value, message.model_dump(mode="json"))
            get_lifecycle_hook_engine().emit(LifecycleEvent.AGENT_MESSAGE,
                                             {"run_id": run_id, "agent_id": agent_id,
                                              "to_agent_id": to_agent_id,
                                              "message_id": message.message_id})
        self._audit("send_message", run_id, agent_id,
                    {"to_agent_id": to_agent_id, "ok": ok, "message_id": message.message_id})
        return ok

    def team_broadcast_message(self, run_id: str, agent_id: str, content: str,
                               title: str = "team_broadcast") -> int:
        self._caller(run_id, agent_id)
        delivered = 0
        for target in self.registry.list_by_run(run_id):
            if target.agent_id != agent_id and self.team_send_message(
                run_id, agent_id, target.agent_id, content, title
            ):
                delivered += 1
        return delivered

    def team_read_messages(self, run_id: str, agent_id: str, max_count: int = 20) -> list[dict[str, Any]]:
        self._caller(run_id, agent_id)
        messages = self.mailbox.receive(agent_id, max_count=max_count)
        self._audit("read_messages", run_id, agent_id, {"count": len(messages)})
        return [message.model_dump(mode="json") for message in messages]

    def team_wait_for_message(self, run_id: str, agent_id: str,
                              timeout: float = 30) -> dict[str, Any] | None:
        self._caller(run_id, agent_id)
        deadline = time.monotonic() + min(max(timeout, 0), 60)
        while time.monotonic() < deadline:
            messages = self.mailbox.receive(agent_id, max_count=1)
            if messages:
                return messages[0].model_dump(mode="json")
            time.sleep(0.05)
        return None

    def team_spawn_teammate(self, run_id: str, agent_id: str,
                            required_capabilities: list[str]) -> dict[str, Any]:
        parent = self._caller(run_id, agent_id)
        self.permissions.authorize(run_id=run_id, agent_id=agent_id,
                                   kind=PermissionKind.TEAMMATE_SPAWN,
                                   operation="team_spawn_teammate",
                                   parameters={"capabilities": required_capabilities})
        agent = self.dynamic_team.spawn(run_id=run_id, team_id=parent.team_id,
                                        required_capabilities=set(required_capabilities),
                                        requested_by=agent_id, parent_agent_id=agent_id)
        return {"agent_id": agent.agent_id, "session_id": agent.session_id,
                "profile_id": agent.profile_id}

    def team_request_teammate_shutdown(self, run_id: str, agent_id: str,
                                       target_agent_id: str, reason: str) -> bool:
        self._caller(run_id, agent_id)
        target = self.registry.get(target_agent_id)
        if target is None or target.run_id != run_id or target_agent_id == agent_id:
            return False
        self._audit("request_teammate_shutdown", run_id, agent_id,
                    {"target_agent_id": target_agent_id, "reason": reason})
        return self.registry.stop(target_agent_id, reason)

    def team_request_permission(self, run_id: str, agent_id: str, kind: str,
                                operation: str, parameters: dict[str, Any] | None = None,
                                reason: str = "") -> dict[str, Any]:
        self._caller(run_id, agent_id)
        request = self.permissions.request(run_id=run_id, agent_id=agent_id,
                                           kind=PermissionKind(kind), operation=operation,
                                           parameters=parameters, reason=reason)
        return request.model_dump(mode="json")

    def team_submit_plan(self, run_id: str, agent_id: str,
                         plan: dict[str, Any]) -> dict[str, Any]:
        self._caller(run_id, agent_id)
        submitted = self.plan_approvals.submit(TeammatePlan(
            run_id=run_id, agent_id=agent_id, **plan,
        ))
        self._audit("submit_plan", run_id, agent_id,
                    {"plan_id": submitted.plan_id, "status": submitted.status.value})
        return submitted.model_dump(mode="json")

    def team_report_progress(self, run_id: str, agent_id: str, task_id: str,
                             progress: float, summary: str) -> bool:
        self._caller(run_id, agent_id)
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        self._audit("report_progress", run_id, agent_id,
                    {"task_id": task_id, "progress": progress, "summary": summary})
        return True


def build_team_tools(service: TeamControlPlaneService, run_id: str, agent_id: str,
                     safety_point: Callable[[], Any] | None = None) -> list[Any]:
    """Build the governed internal tool set with a bound, non-forgeable caller."""
    from langchain.tools import tool

    def before() -> None:
        if safety_point:
            safety_point()

    @tool
    def team_list_members() -> str:
        """列出当前团队成员和生命周期状态。"""
        before(); return json.dumps(service.team_list_members(run_id, agent_id), ensure_ascii=False)

    @tool
    def team_get_member_status(member_agent_id: str) -> str:
        """查询一个团队成员的状态。"""
        before(); return json.dumps(service.team_get_member_status(run_id, agent_id, member_agent_id), ensure_ascii=False)

    @tool
    def team_list_tasks() -> str:
        """列出当前运行的任务板。"""
        before(); return json.dumps(service.team_list_tasks(run_id, agent_id), ensure_ascii=False)

    @tool
    def team_get_task(task_id: str) -> str:
        """读取一个任务详情。"""
        before(); return json.dumps(service.team_get_task(run_id, agent_id, task_id), ensure_ascii=False)

    @tool
    def team_claim_task(task_id: str) -> str:
        """原子认领依赖已满足且能力匹配的任务。"""
        before(); return json.dumps(service.team_claim_task(run_id, agent_id, task_id), ensure_ascii=False)

    @tool
    def team_create_task(task: dict[str, Any], mutation_id: str = "") -> str:
        """通过控制平面创建合法补充任务。"""
        before(); return json.dumps(service.team_create_task(run_id, agent_id, task, mutation_id or None), ensure_ascii=False)

    @tool
    def team_update_task(task_id: str, changes: dict[str, Any], mutation_id: str = "") -> str:
        """更新任务计划字段，不能直接设置成功状态。"""
        before(); return json.dumps(service.team_update_task(run_id, agent_id, task_id, changes, mutation_id or None), ensure_ascii=False)

    @tool
    def team_add_dependency(task_id: str, dependency_id: str, mutation_id: str = "") -> str:
        """经 DAG 校验增加任务依赖。"""
        before(); return json.dumps(service.team_add_dependency(run_id, agent_id, task_id, dependency_id, mutation_id or None), ensure_ascii=False)

    @tool
    def team_mark_blocked(task_id: str, reason: str) -> bool:
        """报告当前任务阻塞。"""
        before(); return service.team_mark_blocked(run_id, agent_id, task_id, reason)

    @tool
    def team_request_replan(reason: str) -> bool:
        """请求 Lead 对当前计划重新规划。"""
        before(); return service.team_request_replan(run_id, agent_id, reason)

    @tool
    def team_send_message(to_agent_id: str, content: str, title: str = "team_message") -> bool:
        """向指定队友发送持久化消息。"""
        before(); return service.team_send_message(run_id, agent_id, to_agent_id, content, title)

    @tool
    def team_broadcast_message(content: str, title: str = "team_broadcast") -> int:
        """向其他队友广播消息。"""
        before(); return service.team_broadcast_message(run_id, agent_id, content, title)

    @tool
    def team_read_messages(max_count: int = 20) -> str:
        """读取自己的团队消息。"""
        before(); return json.dumps(service.team_read_messages(run_id, agent_id, max_count), ensure_ascii=False)

    @tool
    def team_wait_for_message(timeout: float = 30) -> str:
        """短暂等待一条团队消息。"""
        before(); return json.dumps(service.team_wait_for_message(run_id, agent_id, timeout), ensure_ascii=False)

    @tool
    def team_spawn_teammate(required_capabilities: list[str]) -> str:
        """按预算和权限请求创建子队友。"""
        before(); return json.dumps(service.team_spawn_teammate(run_id, agent_id, required_capabilities), ensure_ascii=False)

    @tool
    def team_request_teammate_shutdown(target_agent_id: str, reason: str) -> bool:
        """请求关闭另一个队友。"""
        before(); return service.team_request_teammate_shutdown(run_id, agent_id, target_agent_id, reason)

    @tool
    def team_request_permission(kind: str, operation: str,
                                parameters: dict[str, Any], reason: str = "") -> str:
        """提交结构化权限请求，不能自行批准。"""
        before(); return json.dumps(service.team_request_permission(run_id, agent_id, kind, operation, parameters, reason), ensure_ascii=False)

    @tool
    def team_submit_plan(plan: dict[str, Any]) -> str:
        """提交写操作前的结构化计划。"""
        before(); return json.dumps(service.team_submit_plan(run_id, agent_id, plan), ensure_ascii=False)

    @tool
    def team_report_progress(task_id: str, progress: float, summary: str) -> bool:
        """报告任务进度和证据摘要。"""
        before(); return service.team_report_progress(run_id, agent_id, task_id, progress, summary)

    return [team_list_members, team_get_member_status, team_list_tasks, team_get_task,
            team_claim_task, team_create_task, team_update_task, team_add_dependency,
            team_mark_blocked, team_request_replan, team_send_message,
            team_broadcast_message, team_read_messages, team_wait_for_message,
            team_spawn_teammate, team_request_teammate_shutdown,
            team_request_permission, team_submit_plan, team_report_progress]
