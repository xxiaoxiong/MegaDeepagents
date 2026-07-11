"""TeamRoundExecutor：单轮执行器，被 TeamRunner 和 TeamGraph 共享。

消除两套业务逻辑复制，确保每一步都统一完成：
选择 Agent → 加载 Inbox → 调用 Agent → Action 转 Message
→ MessageBus 发布 → Shared State 更新 → 已读处理
→ State/Round 持久化 → SSE 事件 → termination 判断
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import logger
from app.core.observability import get_current_run_url
from app.multiagent.action_guard import get_effective_allowed_actions
from app.multiagent.agent_spec import AgentSpec, TeamSpec
from app.multiagent.messages import AgentMessage, MessageType
from app.multiagent.review_repair import ReviewRepairLoop, ReviewResult
from app.multiagent.runtime_adapter import AgentRuntimeAdapter
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import SharedTeamState, TeamArtifactRef, TeamPhase
from app.multiagent.termination import TerminationChecker


@dataclass
class RoundResult:
    """单轮执行结果。"""
    speaker: AgentSpec | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    produced_messages: list[AgentMessage] = field(default_factory=list)
    round_number: int = 0
    should_terminate: bool = False
    termination_reason: str | None = None
    termination_phase: TeamPhase | None = None
    error: str | None = None


class TeamRoundExecutor:
    """单轮执行器，封装一轮完整的多 Agent 执行步骤。

    被 TeamRunner（同步主循环）和 TeamGraphRunner（LangGraph 节点）共享调用。
    两个运行时都通过 execute_round() 方法触发一轮完整的业务逻辑。
    """

    def __init__(
        self,
        room: Any,
        adapter: AgentRuntimeAdapter,
        selector: SpeakerSelector,
        termination_checker: TerminationChecker,
        review_loop: ReviewRepairLoop,
        store: Any,
        emitter: Any,
        task_id: str,
        room_id: str,
        team_spec: TeamSpec | None = None,
    ):
        self.room = room
        self.adapter = adapter
        self.selector = selector
        self.termination_checker = termination_checker
        self.review_loop = review_loop
        self.store = store
        self.emitter = emitter
        self.task_id = task_id
        self.room_id = room_id
        self._team_spec = team_spec

    def execute_round(
        self,
        round_number: int,
        last_speaker: str | None = None,
        last_messages: list[AgentMessage] | None = None,
        cancel_check: bool = True,
    ) -> RoundResult:
        """执行一轮完整团队协作。

        Args:
            round_number: 本轮次（调用方递增后传入）
            last_speaker: 上轮选中的 agent 名
            last_messages: 上轮产出的消息列表
            cancel_check: 是否在每轮前检查取消请求

        Returns:
            RoundResult: 本轮执行结果（含终止信号）
        """
        # --- 0. 取消检测（在选取 Agent 之前检查）---
        if cancel_check and self._is_cancel_requested():
            logger.info(f"[TeamRoundExecutor] round {round_number}: cancel requested, terminating")
            self._emit("termination", {"reason": "cancel_requested", "round": round_number})
            return RoundResult(
                round_number=round_number,
                should_terminate=True,
                termination_reason="cancel_requested",
                termination_phase=TeamPhase.CANCELLED,
            )

        # --- 1. 选择发言人 ---
        speaker = self.selector.select(
            shared_state=self.room.state,
            agents=self.room.agents,
            inbox=self.room.inbox,
            last_speaker=last_speaker,
            last_message=last_messages[-1] if last_messages else None,
        )
        if speaker is None:
            logger.info(f"[TeamRoundExecutor] round {round_number}: no speaker selected, terminating")
            self.room.state.update_phase(TeamPhase.FAILED)
            self._emit("termination", {"reason": "no_speaker", "round": round_number})
            return RoundResult(
                round_number=round_number,
                should_terminate=True,
                termination_reason="no_speaker",
                termination_phase=TeamPhase.FAILED,
            )

        self._emit("speaker_selected", {
            "agent": speaker.name, "role": speaker.role, "round": round_number,
        })

        # --- 2. 加载 inbox + state ---
        inbox_context = self.room.inbox.get_relevant_context(speaker.name)
        unread = self.room.inbox.list_unread(speaker.name)

        # --- 3. 构造 prompt + 调用 adapter ---
        actions = self.adapter.run(
            agent=speaker,
            inbox_messages=unread,
            shared_state=self.room.state,
        )

        self._emit("actions_emitted", {
            "agent": speaker.name,
            "round": round_number,
            "action_count": len(actions),
            "action_types": [a.get("type", "?") for a in actions],
        })

        # --- 4. actions → messages ---
        produced_messages = AgentRuntimeAdapter.actions_to_messages(
            agent_name=speaker.name,
            task_id=self.task_id,
            room_id=self.room_id,
            actions=actions,
            round_number=round_number,
        )

        # --- 5. publish to MessageBus ---
        for msg in produced_messages:
            self.room.publish(msg)
            self._emit("message_published", {
                "id": msg.id,
                "from_agent": msg.from_agent,
                "to_agent": msg.to_agent,
                "message_type": msg.message_type.value,
                "content_preview": (msg.content or "")[:200],
                "round": round_number,
            })

        # --- 6. Process actions（更新 SharedTeamState，含 review_result 闭环）---
        self._process_actions(speaker, actions)
        self.store.save_state(self.room.state)

        # --- 7. 标记已读 ---
        for m in unread:
            self.room.inbox.mark_read(m.id, speaker.name)

        # --- 8. Agent 跨任务记忆持久化 ---
        self._persist_agent_memory(speaker, actions, produced_messages)

        # --- 9. 持久化轮次记录 ---
        self._save_round_record(round_number, speaker, actions, produced_messages)

        # --- 10. 生产性投递检测 + 终止判断 ---
        productive_delivery = self._check_productive_delivery(produced_messages)
        decision = self.termination_checker.check(
            state=self.room.state,
            recent_messages=produced_messages,
            round_count=round_number,
            productive_delivery=productive_delivery,
        )

        if decision.should_terminate:
            if decision.final_phase:
                self.room.state.update_phase(decision.final_phase)
            self._emit("termination", {
                "reason": decision.reason,
                "round": round_number,
                "phase": self.room.state.phase.value,
            })

        return RoundResult(
            speaker=speaker,
            actions=actions,
            produced_messages=produced_messages,
            round_number=round_number,
            should_terminate=decision.should_terminate,
            termination_reason=decision.reason,
            termination_phase=decision.final_phase,
        )

    # ========== Action 处理（深层护栏） ==========

    def _process_actions(self, agent: AgentSpec, actions: list[dict[str, Any]]) -> None:
        """根据 actions 更新 SharedTeamState，含角色白名单深层护栏。"""
        if not actions:
            return
        state = self.room.state
        agent_name = agent.name
        allowed_actions = get_effective_allowed_actions(agent)
        allowed_set = set(allowed_actions) if allowed_actions else None

        for action in actions:
            action_type = action.get("type", "no_op")
            # 深层护栏
            if allowed_set is not None and action_type not in allowed_set:
                logger.warning(
                    f"[TeamRoundExecutor] agent={agent_name} action={action_type} "
                    f"不在白名单 {sorted(allowed_set)}，拒绝执行"
                )
                continue

            if action_type == "update_state":
                self._handle_update_state(action, state)
            elif action_type == "create_artifact":
                self._handle_create_artifact(action, agent_name, state)
            elif action_type == "request_review":
                state.review_status = "pending"
                state.update_phase(TeamPhase.REVIEWING)
            elif action_type == "respond_critique":
                pass
            elif action_type == "mark_done":
                self._handle_mark_done(agent, action, state)
            elif action_type == "handoff":
                to_agent = action.get("to_agent", "")
                if to_agent:
                    state.update_phase(TeamPhase.EXECUTING)
            elif action_type == "send_message":
                self._handle_send_message(agent, action, state)

    def _handle_update_state(self, action: dict, state: SharedTeamState) -> None:
        """处理 update_state action。"""
        patch = action.get("patch", {})
        phase = patch.get("phase")
        if phase and state.phase.value != phase:
            try:
                state.update_phase(TeamPhase(phase))
            except ValueError:
                pass
        plan = patch.get("plan")
        if plan:
            if isinstance(plan, list):
                state.plan = "\n".join(
                    f"{s.get('step', '')}. {s.get('content', s.get('action', str(s)))}"
                    if isinstance(s, dict) else str(s)
                    for s in plan
                )
            else:
                state.plan = str(plan)

    def _handle_create_artifact(self, action: dict, agent_name: str, state: SharedTeamState) -> None:
        """处理 create_artifact action。"""
        path = action.get("artifact_path", action.get("content", ""))
        role = action.get("artifact_role", "artifact")
        version = action.get("version", 1)
        artifact_id = action.get("artifact_id")
        if path:
            state.add_artifact(TeamArtifactRef(
                path=path, role=role, produced_by=agent_name,
                version=version, artifact_id=artifact_id,
            ))

    def _handle_mark_done(self, agent: AgentSpec, action: dict, state: SharedTeamState) -> None:
        """处理 mark_done action（仅 Finalizer 允许）。"""
        if agent.role != "Finalizer":
            logger.warning(
                f"[TeamRoundExecutor] agent={agent.name} role={agent.role} "
                f"mark_done 被拒绝（仅 Finalizer 可宣布完成）"
            )
            return
        state.final_output = action.get("content", "")
        state.update_phase(TeamPhase.FINALIZING)

    def _handle_send_message(self, agent: AgentSpec, action: dict, state: SharedTeamState) -> None:
        """处理 send_message，含 review_result 闭环和 final 护栏。"""
        msg_type_str = action.get("message_type", "")
        agent_name = agent.name

        if msg_type_str == "review_result":
            # 深层护栏：只有 Reviewer 角色才能产出 review_result
            if agent.role not in ("ReviewerAgent", "Reviewer"):
                logger.warning(
                    f"[TeamRoundExecutor] agent={agent_name} role={agent.role} "
                    f"越权产出 review_result，拒绝触发返工闭环"
                )
                return
            raw_msg = action.get("content", "")
            review_result = ReviewResult(
                passed=action.get("review_result", {}).get("passed", False),
                issues=action.get("review_result", {}).get("issues", []),
                required_fix_owner=action.get("review_result", {}).get("required_fix_owner"),
                raw=raw_msg,
            )
            # 关键修复：process_review_result 返回的 critique 消息必须发布到 MessageBus
            critique_messages = self.review_loop.process_review_result(
                result=review_result, state=state, room=self.room,
            )
            for critique_msg in critique_messages:
                self.room.publish(critique_msg)
                self._emit("message_published", {
                    "id": critique_msg.id,
                    "from_agent": critique_msg.from_agent,
                    "to_agent": critique_msg.to_agent,
                    "message_type": critique_msg.message_type.value,
                    "content_preview": (critique_msg.content or "")[:200],
                    "round": state.current_round,
                })

            # 同步 artifact 评审状态
            artifact_refs = action.get("artifact_refs", []) or []
            for ref in artifact_refs:
                path = ref.get("path") if isinstance(ref, dict) else None
                if path:
                    status = "approved" if review_result.passed else "rejected"
                    state.mark_artifact_reviewed(
                        path=path, reviewed_by=agent_name, status=status,
                    )

        elif msg_type_str == "plan" and state.phase == TeamPhase.PLANNING:
            state.update_phase(TeamPhase.EXECUTING)
        elif msg_type_str == "delegation" and state.phase in (TeamPhase.PLANNING, TeamPhase.DISCUSSING):
            state.update_phase(TeamPhase.EXECUTING)
        elif msg_type_str == "final":
            if agent.role != "Finalizer":
                logger.warning(
                    f"[TeamRoundExecutor] agent={agent_name} role={agent.role} "
                    f"越权发 final 消息，不写入 final_output"
                )
                return
            state.final_output = action.get("content", "") or state.final_output
            state.update_phase(TeamPhase.FINALIZING)

    # ========== 辅助方法 ==========

    def _check_productive_delivery(self, messages: list[AgentMessage]) -> bool:
        """判断本轮产出是否有消息到达真实 Agent inbox。"""
        if not messages:
            return False
        agent_names = {a.name for a in self.room.agents}
        for msg in messages:
            if msg.message_type == MessageType.NO_OP:
                continue
            if msg.message_type in (MessageType.STATE_UPDATE, MessageType.ARTIFACT_CREATED):
                return True
            if isinstance(msg.to_agent, str) and msg.to_agent:
                if msg.to_agent in agent_names:
                    return True
                if (msg.metadata or {}).get("routing_fallback"):
                    return True
                continue
            # broadcast：检查是否有非自己的订阅者
            subs = self.room.bus.get_subscriptions_for_message(msg)
            if subs:
                for sub_name in subs:
                    if sub_name != msg.from_agent and sub_name in agent_names:
                        return True
            else:
                return True
        return False

    def _persist_agent_memory(self, agent: AgentSpec, actions: list[dict], messages: list[AgentMessage]) -> None:
        """Agent 跨任务记忆持久化（B3）。"""
        if not agent or not actions:
            return
        scope = agent.private_memory_scope or agent.name
        try:
            from app.multiagent.layered_memory import get_layered_memory, MemoryTier
            memory = get_layered_memory()
        except Exception:
            return
        for action in actions:
            try:
                atype = action.get("type", "no_op")
                content = (action.get("content") or "").strip()
                if atype in ("create_artifact", "request_review", "respond_critique", "mark_done"):
                    target = action.get("to_agent") or action.get("artifact_role") or "?"
                    summary = f"[{atype}] -> {target}: {content[:160] or '(无内容)'}"
                    entry_id = f"proc_{scope}_{hash(summary) & 0xFFFFFFFF:x}"
                    existing = memory.procedural.get(entry_id)
                    if existing is None:
                        memory.add(MemoryTier.PROCEDURAL, content=summary, agent_scope=scope,
                                   importance=0.6, metadata={"source_action": atype, "id": entry_id, "task_id": self.task_id},
                                   task_id=self.task_id)
                    else:
                        existing.importance = min(1.0, existing.importance + 0.05)
                        memory.procedural._persist(existing, task_id=self.task_id)
                elif atype == "send_message" and len(content) >= 30:
                    summary = f"[send_to:{action.get('to_agent','?')}] {content[:200]}"
                    entry_id = f"sem_{scope}_{hash(summary) & 0xFFFFFFFF:x}"
                    existing = memory.semantic.get(entry_id)
                    if existing is None:
                        memory.add(MemoryTier.SEMANTIC, content=summary, agent_scope=scope,
                                   importance=0.5, metadata={"source_action": atype, "id": entry_id, "task_id": self.task_id},
                                   task_id=self.task_id)
                    else:
                        existing.importance = min(1.0, existing.importance + 0.05)
                        memory.semantic._persist(existing, task_id=self.task_id)
            except Exception:
                pass

    def _save_round_record(self, round_number: int, speaker: AgentSpec, actions: list[dict], messages: list[AgentMessage]) -> None:
        """持久化轮次记录。"""
        msg_ids = [m.id for m in messages]
        action_summary = "; ".join(
            f"{a.get('type', '?')}({'->' + a.get('to_agent', '') if a.get('to_agent') else ''})"
            for a in actions[:5]
        )
        run_url = get_current_run_url()
        self.store.save_round(
            room_id=self.room_id,
            round_number=round_number,
            selected_speaker=speaker.name,
            action_summary=action_summary[:200],
            message_ids=msg_ids,
            langsmith_run_url=run_url,
        )

    def _is_cancel_requested(self) -> bool:
        """检查取消状态（持久化的 cancel_requested 标记）。"""
        if self.room.state.metadata.get("cancel_requested"):
            return True
        if hasattr(self.room, "is_cancel_requested") and self.room.is_cancel_requested():
            return True
        return False

    def _emit(self, event: str, payload: dict) -> None:
        """发送 SSE 事件。"""
        try:
            self.emitter.emit(self.room_id or "", event, payload)
        except Exception:
            pass
