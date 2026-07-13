"""Mailbox — Agent 间可信消息邮箱。

Phase F（docs/MegaDeepagents_Agent_Teams_改造任务书.md §14）：
- 每个 AgentInstance 拥有独立 Inbox
- 跨 Agent 消息落表（持久化层在 Phase G SQLite）
- 治理钩子：审计 / 黑白名单 / REQUEST 出站需要 contract method 签名
- 支持 broadcast / send / broadcast_to_role

设计：
- 进程内优先；并发安全（threading.Lock）
- 不依赖 messages.py 中的 AgentMessage（保留向后兼容）；新建 MailboxMessage 模型
- 支持 reply_to / thread_id（用于多 Agent 协商）
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from app.core.logging import logger
from pydantic import BaseModel, Field


class MessageSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class MailboxMessage(BaseModel):
    """Mailbox 上的消息。"""
    message_id: str
    from_agent_id: str
    from_agent_name: str = ""
    from_role: str = ""

    to_agent_id: str | None = None  # None = broadcast
    to_role: str | None = None  # role 级广播

    run_id: str
    title: str
    content: str
    severity: MessageSeverity = MessageSeverity.INFO

    # 协商线索
    thread_id: str | None = None
    reply_to: str | None = None

    # 治理
    delivery_attempts: int = 0
    consumed_at: datetime | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    CONSUMED = "consumed"
    FAILED = "failed"


class MailboxError(Exception):
    pass


class PolicyViolation(MailboxError):
    """治理策略拦截（如黑名单、频率限制、缺字段）。"""


# ===== 治理钩子接口 =====

PolicyHook = Callable[[MailboxMessage], None]


class Mailbox:
    """每个 Agent 的独立 inbox + 跨 Agent 治理。

    Phase E（in-memory）：
    - send(message) → 投递到目标 Agent inbox
    - broadcast_run(message) → 投递到 run 内所有 Agent
    - broadcast_role(message) → 投递到 run 内某 role 的所有 Agent

    治理（Phase F 第 5 步）：
    - allow_send policy：可注册 send 前钩子（拒绝、加签等）
    - 黑名单（from_agent_id 维度）：block / unblock
    - 反压：inbox 容量上限（per_agent_max_size）+ discard_oldest 策略

    持久化（Phase G）：
    - SqliteSaver 模式时落表 mailbox_messages，restart 时 inbox 自动恢复
    """

    def __init__(
        self,
        per_agent_max_size: int = 200,
        default_policy: PolicyHook | None = None,
    ) -> None:
        self._inboxes: dict[str, deque[MailboxMessage]] = defaultdict(deque)
        self._capacity = per_agent_max_size
        self._lock = threading.RLock()

        # 治理
        self._policy_hooks: list[PolicyHook] = []
        if default_policy:
            self._policy_hooks.append(default_policy)
        self._blocklist: set[str] = set()
        self._rate: dict[str, list[float]] = defaultdict(list)
        self._rate_limit_per_minute = 60

        # 顺序读出（绕内存即可）
        self._all_messages: dict[str, MailboxMessage] = {}

    # ===== 治理 API =====

    def add_policy_hook(self, hook: PolicyHook) -> None:
        self._policy_hooks.append(hook)

    def block(self, agent_id: str) -> None:
        self._blocklist.add(agent_id)
        logger.warning(f"[Mailbox] blocked agent={agent_id}")

    def unblock(self, agent_id: str) -> None:
        self._blocklist.discard(agent_id)

    def is_blocked(self, agent_id: str) -> bool:
        return agent_id in self._blocklist

    # ===== 投递 =====

    def send(self, message: MailboxMessage) -> bool:
        """投递一条消息（任意目标）。返回是否成功（治理拦截返回 False）。"""
        # 治理钩子（可能抛 PolicyViolation）
        try:
            for hook in self._policy_hooks:
                hook(message)
        except PolicyViolation as exc:
            logger.warning(f"[Mailbox] policy violation blocked msg={message.message_id}: {exc}")
            return False
        # 黑名单
        if self.is_blocked(message.from_agent_id):
            logger.warning(f"[Mailbox] blocked sender={message.from_agent_id}")
            return False
        # 频率限制（per from_agent_id per minute）
        if not self._rate_check(message.from_agent_id):
            logger.warning(f"[Mailbox] rate limited sender={message.from_agent_id}")
            return False
        with self._lock:
            self._enforce_capacity(message.to_agent_id)
            self._inboxes[message.to_agent_id].append(message)
            self._all_messages[message.message_id] = message
            logger.info(
                f"[Mailbox] delivered msg={message.message_id} "
                f"from={message.from_agent_id} → {message.to_agent_id}"
            )
        return True

    def broadcast_run(self, run_id: str, agent_ids: list[str], message: MailboxMessage) -> int:
        """广播到 run 内所有 agent。返回实际投递数。"""
        delivered = 0
        for agent_id in agent_ids:
            if agent_id == message.from_agent_id:
                continue  # 跳过自己
            cloned = message.model_copy(update={
                "to_agent_id": agent_id,
                "message_id": make_message_id(),
            })
            if self.send(cloned):
                delivered += 1
        return delivered

    def broadcast_role(
        self,
        run_id: str,
        role: str,
        target_agents: list[tuple[str, str]],  # (agent_id, agent_role)
        message: MailboxMessage,
    ) -> int:
        """递送到 run 内某 role 的所有 Agent。target_agents=[(id,role),...]。"""
        ids = [a_id for a_id, a_role in target_agents if a_role == role and a_id != message.from_agent_id]
        return self.broadcast_run(run_id, ids, message)

    # ===== 取信 =====

    def receive(self, agent_id: str, max_count: int = 10) -> list[MailboxMessage]:
        """从 inbox 取出最多 max_count 条消息。FIFO。"""
        out: list[MailboxMessage] = []
        with self._lock:
            inbox = self._inboxes.get(agent_id)
            if not inbox:
                return out
            while inbox and len(out) < max_count:
                msg = inbox.popleft()
                msg.consumed_at = datetime.utcnow()
                out.append(msg)
        return out

    def peek(self, agent_id: str) -> list[MailboxMessage]:
        """只看不消费。"""
        with self._lock:
            inbox = self._inboxes.get(agent_id)
            return list(inbox) if inbox else []

    def inbox_size(self, agent_id: str) -> int:
        with self._lock:
            return len(self._inboxes.get(agent_id, deque()))

    # ===== Idle Agent 唤醒（任务书 §12）=====

    def notify_idle(self, agent_id: str, hint: str = "") -> bool:
        """向 idle agent 投递一条 wakeup 提示消息（SOFTWARE_INTERRUPT 风格）。

        场景：ParallelTeamScheduler 在没有 idle worker 时，
        可以向 RUNNING agent 的 inbox 投递一条提示，鼓励其尽快完成或主动释放。
        若 agent 不存在/已 stopped，仍投递（这是提示而非 RPC，保持单向语义）。

        Returns True 表示成功投递（治理未拦截）。
        """
        wakeup = MailboxMessage(
            message_id=make_message_id(),
            from_agent_id="orchestrator",
            from_agent_name="Orchestrator",
            from_role="system",
            to_agent_id=agent_id,
            run_id="",  # 调用方可后续 set
            title="wakeup_hint",
            content=hint or "请尽快完成当前任务或主动让出，以释放并发槽位。",
            severity=MessageSeverity.INFO,
            metadata={"kind": "wakeup", "hint": hint},
        )
        return self.send(wakeup)

    def wake_idle_agents(
        self,
        run_id: str,
        agent_ids: list[str],
        hint: str = "",
    ) -> int:
        """批量唤醒：向一组 agent 投递 wakeup 消息。返回成功投递数。

        用于 ParallelTeamScheduler 在没有 idle worker 时触发溢出策略。
        跳过黑名单与频率限制失败的 agent。
        """
        delivered = 0
        for agent_id in agent_ids:
            msg = MailboxMessage(
                message_id=make_message_id(),
                from_agent_id="orchestrator",
                from_agent_name="Orchestrator",
                from_role="system",
                to_agent_id=agent_id,
                run_id=run_id,
                title="wakeup_hint",
                content=hint or "请尽快完成当前任务或主动让出，以释放并发槽位。",
                severity=MessageSeverity.INFO,
                metadata={"kind": "wakeup", "hint": hint},
            )
            if self.send(msg):
                delivered += 1
        return delivered

    # ===== 列表 =====

    def list_messages_in_run(self, run_id: str) -> list[MailboxMessage]:
        return [m for m in self._all_messages.values() if m.run_id == run_id]

    def get_message(self, message_id: str) -> MailboxMessage | None:
        return self._all_messages.get(message_id)

    # ===== 持久化桥（Phase G）=====

    def snapshot(self) -> dict[str, Any]:
        """序列化用于检查点存储（Phase G）。"""
        with self._lock:
            data = {
                "inboxes": {aid: [m.model_dump() for m in dq] for aid, dq in self._inboxes.items()},
                "blocklist": list(self._blocklist),
                "all": {mid: m.model_dump() for mid, m in self._all_messages.items()},
            }
        return data

    def restore(self, snapshot: dict[str, Any]) -> None:
        """从快照恢复（Phase G）。"""
        with self._lock:
            self._inboxes.clear()
            for aid, qsnap in (snapshot.get("inboxes") or {}).items():
                dq: deque[MailboxMessage] = deque()
                for msnap in qsnap:
                    dq.append(MailboxMessage(**msnap))
                self._inboxes[aid] = dq
            self._blocklist = set(snapshot.get("blocklist") or [])
            self._all_messages = {
                mid: MailboxMessage(**msnap)
                for mid, msnap in (snapshot.get("all") or {}).items()
            }

    # ===== SQLite 落库（Phase G 第 2 步）=====

    def flush_to_db(self, run_id: str, history=None) -> int:
        """把当前内存中（已 delivered 但未 consumed）的消息全部 flush 到 sqlite。

        返回落库条数。幂等：每条按 message_id upsert。
        """
        from app.multiagent.phase_g_store import get_agent_run_history
        h = history or get_agent_run_history()
        count = 0
        with self._lock:
            for msg in list(self._all_messages.values()):
                if msg.run_id != run_id:
                    continue
                try:
                    h.insert_mailbox_message(
                        message_id=msg.message_id,
                        from_agent_id=msg.from_agent_id,
                        from_agent_name=msg.from_agent_name,
                        from_role=msg.from_role,
                        to_agent_id=msg.to_agent_id,
                        to_role=msg.to_role,
                        run_id=msg.run_id,
                        title=msg.title,
                        content=msg.content,
                        severity=msg.severity.value if isinstance(msg.severity, MessageSeverity) else str(msg.severity),
                        thread_id=msg.thread_id,
                        reply_to=msg.reply_to,
                        delivery_attempts=msg.delivery_attempts,
                        consumed_at=msg.consumed_at,
                        status="consumed" if msg.consumed_at else "delivered",
                        created_at=msg.created_at,
                        metadata=msg.metadata,
                    )
                    count += 1
                except Exception as exc:
                    logger.warning(f"[Mailbox] flush_to_db msg={msg.message_id} 失败: {exc}")
        logger.info(f"[Mailbox] flush_to_db run={run_id} 落库 {count} 条")
        return count

    def restore_from_db(self, run_id: str, history=None) -> int:
        """从 sqlite 加载该 run 内全部消息，重建 inbox 与 _all_messages。

        返回恢复条数。已 consumed 的不再塞回 inbox（避免重复消费）。
        """
        from app.multiagent.phase_g_store import get_agent_run_history
        h = history or get_agent_run_history()
        rows = h.list_mailbox_messages(run_id=run_id)
        count = 0
        with self._lock:
            for r in rows:
                try:
                    msg = MailboxMessage(
                        message_id=r["message_id"],
                        from_agent_id=r["from_agent_id"],
                        from_agent_name=r.get("from_agent_name") or "",
                        from_role=r.get("from_role") or "",
                        to_agent_id=r.get("to_agent_id"),
                        to_role=r.get("to_role"),
                        run_id=r["run_id"],
                        title=r.get("title") or "",
                        content=r.get("content") or "",
                        severity=MessageSeverity(r.get("severity") or "info"),
                        thread_id=r.get("thread_id"),
                        reply_to=r.get("reply_to"),
                        delivery_attempts=r.get("delivery_attempts") or 0,
                        consumed_at=datetime.fromisoformat(r["consumed_at"]) if r.get("consumed_at") else None,
                        created_at=datetime.fromisoformat(r["created_at"]) if r.get("created_at") else datetime.utcnow(),
                        metadata=json.loads(r.get("metadata") or "{}"),
                    )
                except Exception as exc:
                    logger.warning(f"[Mailbox] restore_from_db msg={r.get('message_id')} 失败: {exc}")
                    continue
                self._all_messages[msg.message_id] = msg
                # 已 consumed 的不塞回 inbox（防止重复消费）
                if msg.consumed_at is None and msg.to_agent_id is not None:
                    self._inboxes[msg.to_agent_id].append(msg)
                count += 1
            # 黑名单（cover：从 mailbox_messages 中无法直接恢复，需单独表；
            # 这里 fall back：从所有 from_agent_id 反推不出黑名单，保持空）
        logger.info(f"[Mailbox] restore_from_db run={run_id} 恢复 {count} 条")
        return count

    # ===== 私有 =====

    def _enforce_capacity(self, agent_id: str | None) -> None:
        if agent_id is None:
            return
        inbox = self._inboxes[agent_id]
        while len(inbox) >= self._capacity:
            dropped = inbox.popleft()
            logger.warning(
                f"[Mailbox] inbox overflow for agent={agent_id}, dropped old msg={dropped.message_id}"
            )

    def _rate_check(self, agent_id: str) -> bool:
        import time
        now = time.time()
        recent = self._rate[agent_id]
        # 清理超 60s 历史
        while recent and (now - recent[0]) > 60:
            recent.pop(0)
        if len(recent) >= self._rate_limit_per_minute:
            return False
        recent.append(now)
        return True


def make_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:12]


def make_thread_id() -> str:
    return "thr_" + uuid.uuid4().hex[:12]


# ===== 全局单例 =====

_mailbox: Mailbox | None = None


def get_mailbox() -> Mailbox:
    global _mailbox
    if _mailbox is None:
        _mailbox = Mailbox()
    return _mailbox


def reset_mailbox() -> None:
    global _mailbox
    _mailbox = None
