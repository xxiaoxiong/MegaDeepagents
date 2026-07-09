"""Agent 执行封装：运行任务并处理 HITL。"""

import asyncio
import time
import traceback
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from app.core.config import settings
from app.core.logging import logger
from app.core.schemas import TaskStatus
from app.task.models import TaskEvent
from app.task.service import get_task_service


# 全局 pending runners，用于 HITL 恢复，值为 (runner, timestamp)
_pending_runners: dict[str, tuple[Any, float]] = {}


def get_pending_runner(task_id: str) -> Any | None:
    entry = _pending_runners.get(task_id)
    if entry is None:
        return None
    runner, _ = entry
    return runner


def set_pending_runner(task_id: str, runner: Any) -> None:
    _pending_runners[task_id] = (runner, time.time())


def remove_pending_runner(task_id: str) -> None:
    _pending_runners.pop(task_id, None)


def cleanup_stale_runners(timeout_minutes: int | None = None) -> int:
    """清理超过 TTL 的 pending runners，返回清理数量。"""
    ttl = timeout_minutes or settings.pending_runner_ttl_minutes
    now = time.time()
    stale_ids = [
        tid for tid, (_, ts) in _pending_runners.items()
        if (now - ts) > ttl * 60
    ]
    for tid in stale_ids:
        remove_pending_runner(tid)
        logger.warning(f"Stale runner cleaned up: task_id={tid}, ttl={ttl}min")
    return len(stale_ids)


# SSE 流式队列管理
_task_stream_queues: dict[str, asyncio.Queue] = {}


def get_stream_queue(task_id: str) -> asyncio.Queue | None:
    return _task_stream_queues.get(task_id)


def set_stream_queue(task_id: str, queue: asyncio.Queue) -> None:
    _task_stream_queues[task_id] = queue


def remove_stream_queue(task_id: str) -> None:
    _task_stream_queues.pop(task_id, None)


class TaskRunner:
    def __init__(self, task_service, task_id: str | None = None, thread_id: str = "default", auto_approve: bool = True):
        self.task_service = task_service
        self.task_id = task_id
        self.thread_id = thread_id
        self.auto_approve = auto_approve
        self._pending_agent = None
        self._pending_config = None

    @staticmethod
    def _count_action_requests(interrupt_data: list[Any]) -> int:
        count = 0
        for interrupt in interrupt_data:
            if hasattr(interrupt, "value") and isinstance(interrupt.value, dict):
                count += len(interrupt.value.get("action_requests", []))
        return count

    def _record_event(self, event_type: str, data: dict[str, Any]) -> None:
        if self.task_id:
            self.task_service.add_event(self.task_id, TaskEvent(
                event_type=event_type,
                data=data,
            ))

    def _save_messages(self, result: Any) -> None:
        """从最终结果中提取消息并写入数据库。"""
        if not self.task_id:
            return
        final_value = getattr(result, "value", result)
        messages = []
        if isinstance(final_value, dict):
            messages = final_value.get("messages", [])
        if not messages:
            return

        for msg in messages:
            msg_type = None
            content = ""
            extra: dict[str, Any] = {}

            if isinstance(msg, dict):
                msg_type = msg.get("type") or msg.get("role")
                content = msg.get("content") or ""
                extra = {"agent": msg.get("lc_agent_name", "coordinator")}
            else:
                msg_type = getattr(msg, "type", getattr(msg, "role", None))
                content = getattr(msg, "content", "") or ""
                extra = {"agent": getattr(msg, "lc_agent_name", "coordinator")}

            if msg_type in ("tool", "function"):
                name = getattr(msg, "name", getattr(msg, "tool_name", "tool")) if not isinstance(msg, dict) else msg.get("name", "tool")
                extra["name"] = name
                extra["status"] = "success"
                self.task_service.add_message(self.task_id, "tool", content or "(工具执行完毕，无返回内容)", extra)
            elif msg_type == "assistant":
                self.task_service.add_message(self.task_id, "assistant", content, extra)
            elif msg_type == "system":
                self.task_service.add_message(self.task_id, "system", content, extra)
            # human 消息在入口处已记录，跳过

    def _auto_register_artifacts(self) -> None:
        """任务完成后自动扫描 workspace 下产物并注册为 artifact。"""
        if not self.task_id:
            return
        try:
            workspace = Path(settings.workspace_dir)
            if not workspace.exists():
                return
            existing = {a.path for a in self.task_service.get_artifacts(self.task_id)}
            seen = set(existing)
            task = self.task_service.get_task(self.task_id)
            task_start = task.created_at.timestamp() if task and task.created_at else 0
            ARTIFACT_EXTS = {
                '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.json',
                '.md', '.txt', '.csv', '.sql', '.yaml', '.yml', '.toml',
                '.sh', '.bat', '.ps1', '.log', '.xml', '.ini', '.cfg',
                '.rst', '.pdf', '.docx', '.xlsx', '.pptx', '.zip', '.tar', '.gz',
                '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico',
            }
            IGNORED_DIRS = {'__pycache__', '.git', 'node_modules', '.venv', 'venv', 'dist', 'build', '.idea', '.vscode'}
            MAX_ARTIFACTS = 50
            count = 0
            for file_path in workspace.rglob("*"):
                if not file_path.is_file():
                    continue
                if file_path.stat().st_mtime < task_start:
                    continue
                if any(part.startswith('.') for part in file_path.relative_to(workspace).parts):
                    continue
                if any(part in IGNORED_DIRS for part in file_path.relative_to(workspace).parts):
                    continue
                rel = file_path.relative_to(workspace)
                path_str = str(rel)
                if path_str in seen:
                    continue
                ext = file_path.suffix.lower()
                if ext and ext not in ARTIFACT_EXTS:
                    continue
                seen.add(path_str)
                try:
                    size = file_path.stat().st_size
                except OSError:
                    continue
                self.task_service.add_artifact(
                    self.task_id,
                    path_str,
                    file_path.name,
                    size,
                )
                logger.info(f"Auto-registered artifact: task={self.task_id}, path={path_str}")
                count += 1
                if count >= MAX_ARTIFACTS:
                    break
        except Exception as exc:
            logger.warning(f"Auto-register artifacts failed: {exc}")

    def run(self, user_input: str) -> dict[str, Any]:
        """执行任务，返回结果字典。"""
        from app.core.agent_factory import build_agent
        from app.core.context import AgentContext

        if not self.task_id:
            task = self.task_service.create_task(user_input, self.thread_id)
            self.task_id = task.task_id
            self._record_event("runner_started", {"user_input": user_input})
            self.task_service.add_message(self.task_id, "user", user_input, {"user_input": user_input})
            self.task_service.update_status(task.task_id, TaskStatus.RUNNING)

        agent = build_agent(task_id=self.task_id, thread_id=self.thread_id, auto_approve=self.auto_approve)
        context = AgentContext(user_id=self.thread_id or "default", request_id=self.task_id or "")
        config = {
            "configurable": {"thread_id": self.thread_id},
            "context": context,
        }

        try:
            self._record_event("agent_invoke_start", {})
            result = agent.invoke(
                {"messages": [("user", user_input)]},
                config,
            )
            self._record_event("agent_invoke_done", {})

            # 任务正常完成
            self._save_messages(result)
            content = self._normalize_content(result)
            self.task_service.add_message(self.task_id, "assistant", content, {})
            self._record_event("task_completed", {"content_length": len(content)})
            self.task_service.mark_completed(self.task_id, content)
            self._auto_register_artifacts()
            remove_stream_queue(self.task_id)
            logger.info(f"Task completed: {self.task_id}")
            return {
                "status": "completed",
                "task_id": self.task_id,
                "content": content,
            }
        except GraphInterrupt as exc:
            # 框架原生 HITL 中断
            interrupts = exc.args[0] if exc.args else []
            action_count = self._count_action_requests(interrupts)
            self._record_event("graph_interrupt", {"args_count": len(interrupts), "action_count": action_count})
            if not self.auto_approve:
                self._pending_agent = agent
                self._pending_config = config
                set_pending_runner(self.task_id, self)
                self.task_service.mark_waiting_approval(self.task_id)
                logger.info(f"Task waiting approval: {self.task_id}")
                return {
                    "status": "waiting_approval",
                    "task_id": self.task_id,
                    "thread_id": self.thread_id,
                    "interrupts": interrupts,
                }
            # auto_approve=True 时不应走到这里
            logger.warning(f"Unexpected GraphInterrupt with auto_approve=True for {self.task_id}, auto-resuming")
            result = self._resume_auto(agent, config, interrupts)
            remove_stream_queue(self.task_id)
            return result
        except Exception as exc:
            error_msg = str(exc)
            try:
                self.task_service.add_message(self.task_id, "system", f"执行失败: {error_msg}", {"error": error_msg})
                self._record_event("task_failed", {"error": error_msg})
                self.task_service.mark_failed(self.task_id, error_msg)
            except Exception as store_exc:
                logger.error(f"Store failed: {store_exc}\n{traceback.format_exc()}")
            remove_stream_queue(self.task_id)
            logger.error(f"Task runner failed: {exc}\n{traceback.format_exc()}")
            return {
                "status": "failed",
                "task_id": self.task_id,
                "error": error_msg,
            }

    def approve(self, max_sequential_interrupts: int = 20) -> dict[str, Any]:
        """自动批准当前待审批操作并恢复执行。"""
        if not self._pending_agent or not self._pending_config:
            raise RuntimeError("No pending agent to approve")
        for attempt in range(max_sequential_interrupts):
            action_count = self._count_pending_actions()
            decisions = [{"type": "approve"} for _ in range(action_count)]
            try:
                return self.resume(decisions)
            except GraphInterrupt as gi:
                if attempt >= max_sequential_interrupts - 1:
                    raise RuntimeError(f"Too many sequential interrupts for task {self.task_id}") from gi
                continue
            except ValueError as ve:
                err_msg = str(ve)
                if "does not match" in err_msg and "number of hanging tool calls" in err_msg:
                    import re
                    match = re.search(r"number of hanging tool calls \((\d+)\)", err_msg)
                    if match:
                        action_count = int(match.group(1))
                        continue
                raise

    def _count_pending_actions(self) -> int:
        events = self.task_service.get_events(self.task_id)
        for ev in reversed(events):
            if ev.event_type in ("interrupt_detected", "graph_interrupt"):
                return ev.data.get("action_count", 1)
        return 1

    def _resume_auto(self, agent: Any, config: dict, interrupts: list[Any]) -> dict[str, Any]:
        """自动批准所有待审批操作并继续执行。"""
        decisions_map: dict[str, list[dict]] = {}
        for i, interrupt in enumerate(interrupts):
            if hasattr(interrupt, 'value') and isinstance(interrupt.value, dict):
                interrupt_id = getattr(interrupt, 'id', f'__interrupt_{i}')
                action_requests = interrupt.value.get("action_requests", [])
                decisions_map[interrupt_id] = [{"type": "approve"} for _ in action_requests]
        if not decisions_map:
            raise RuntimeError("No action requests found in interrupt")
        if len(decisions_map) == 1:
            resume_data = {"decisions": next(iter(decisions_map.values()))}
        else:
            resume_data = {k: {"decisions": v} for k, v in decisions_map.items()}

        result = agent.invoke(
            Command(resume=resume_data),
            config,
        )
        content = self._normalize_content(result)
        self._record_event("task_completed_auto", {"content_length": len(content)})
        self.task_service.mark_completed(self.task_id, content)
        self._auto_register_artifacts()
        remove_stream_queue(self.task_id)
        logger.info(f"Task completed (auto-resumed): {self.task_id}")
        return {
            "status": "completed",
            "task_id": self.task_id,
            "content": content,
        }

    def resume(self, decisions: list[dict] | dict[str, list[dict]]) -> dict[str, Any]:
        """从 HITL 中断点恢复执行。"""
        if not self._pending_agent or not self._pending_config:
            raise RuntimeError("No pending agent to resume")
        agent = self._pending_agent
        config = self._pending_config

        if isinstance(decisions, dict):
            resume_data = {k: {"decisions": v} for k, v in decisions.items()}
            total_count = sum(len(v) for v in decisions.values())
        else:
            resume_data = {"decisions": decisions}
            total_count = len(decisions)

        self._record_event("resume_started", {"decisions_count": total_count})
        try:
            result = agent.invoke(
                Command(resume=resume_data),
                config,
            )
        except GraphInterrupt as exc:
            interrupts = exc.args[0] if exc.args else []
            action_count = self._count_action_requests(interrupts)
            self._record_event("interrupt_detected", {"count": len(interrupts), "action_count": action_count})
            raise RuntimeError("Resume resulted in another interrupt")
        if isinstance(result, dict):
            updates = result.get('updates', {})
            if '__interrupt__' in updates:
                raise RuntimeError("Resume resulted in another interrupt")
        content = self._normalize_content(result)
        self.task_service.add_message(self.task_id, "assistant", content, {"resumed": True})
        self._record_event("task_completed_resumed", {"content_length": len(content)})
        self.task_service.mark_completed(self.task_id, content)
        self._auto_register_artifacts()
        remove_stream_queue(self.task_id)
        logger.info(f"Task resumed and completed: {self.task_id}")
        self._pending_agent = None
        self._pending_config = None
        return {
            "status": "completed",
            "task_id": self.task_id,
            "content": content,
        }

    @staticmethod
    def _is_human_message(msg: Any) -> bool:
        if isinstance(msg, dict):
            role = msg.get("type") or msg.get("role")
            return role in ("human", "user")
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if role:
            return role in ("human", "user")
        cls_name = type(msg).__name__
        return "HumanMessage" in cls_name or "User" in cls_name

    def _normalize_content(self, result: Any) -> str:
        """提取最终文本内容。兼容结构化响应（TaskResult JSON）。"""
        content = ""
        final_value = getattr(result, "value", result)
        raw_msg = None

        if isinstance(final_value, dict):
            messages = final_value.get("messages", [])
            if messages:
                for msg in reversed(messages):
                    if self._is_human_message(msg):
                        continue
                    msg_content = ""
                    if hasattr(msg, "content"):
                        msg_content = msg.content or ""
                    elif isinstance(msg, dict):
                        msg_content = msg.get("content", "") or ""
                    if msg_content:
                        raw_msg = msg_content
                        break
                if not raw_msg:
                    for msg in reversed(messages):
                        if self._is_human_message(msg):
                            continue
                        raw_msg = getattr(msg, "content", "") or str(msg)
                        if isinstance(msg, dict) and not raw_msg:
                            raw_msg = msg.get("content", "") or str(msg)
                        if raw_msg:
                            break
        else:
            raw_msg = str(final_value)

        if not raw_msg and self.task_id:
            try:
                db_messages = self.task_service.get_messages(self.task_id)
                for msg in reversed(db_messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        raw_msg = msg["content"]
                        break
            except Exception:
                pass

        if raw_msg and settings.enable_response_format:
            try:
                parsed = TaskResult.model_validate_json(raw_msg)
                content = parsed.summary
                if parsed.artifacts and self.task_id:
                    self._register_structured_artifacts(parsed.artifacts)
            except Exception:
                content = raw_msg
        else:
            content = raw_msg or ""

        return content

    def _register_structured_artifacts(self, artifact_paths: list[str]) -> None:
        if not self.task_id:
            return
        workspace = Path(settings.workspace_dir)
        existing = {a.path for a in self.task_service.get_artifacts(self.task_id)}
        for rel_path in artifact_paths:
            if rel_path in existing:
                continue
            try:
                full_path = (workspace / rel_path).resolve()
                if not str(full_path).startswith(str(workspace.resolve())):
                    continue
                size = full_path.stat().st_size if full_path.is_file() else 0
                self.task_service.add_artifact(
                    self.task_id,
                    rel_path,
                    full_path.name,
                    size,
                )
                logger.info(f"Auto-registered structured artifact: task={self.task_id}, path={rel_path}")
            except OSError:
                continue
