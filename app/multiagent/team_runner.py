"""TeamRunner：多智能体团队运行核心循环。

核心流程：
1. create room（或 load 已有 room）
2. 初始化 MessageBus / AgentInbox / SharedTeamState
3. publish user_request 到总线
4. loop:
   a. SpeakerSelector 选择下一 Agent
   b. 加载该 Agent 的 inbox + shared_state
   c. AgentRuntimeAdapter.run() 产生 actions
   d. actions → AgentMessages → bus.publish+write
   e. 更新 SharedTeamState
   f. 发 task events
   g. TerminationChecker 判断
5. finalize

注意：
- 本 runner 目前是"半模拟"模式：Agent 不真正调用 LLM，而是通过 AgentRuntimeAdapter
  在 prompt 阶段返回 no_op。真实 LLM 调用需要接入后替换 runtime_adapter。
- 每个 action 都会落库，供前端查看 step-by-step。
"""

from __future__ import annotations

import traceback
import uuid
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.core.observability import traceable, trace_span, emit_trace_event, get_current_run_url
from app.multiagent.action_guard import (
    get_effective_allowed_actions,
    is_action_allowed,
)
from app.multiagent.agent_spec import (
    AgentSpec,
    TeamRunConfig,
    TeamRunResult,
    TeamSpec,
)
from app.multiagent.default_teams import get_team, list_teams as _list_teams
from app.multiagent.event_emitter import get_event_emitter
from app.multiagent.inbox import AgentInbox
from app.multiagent.messages import (
    AgentMessage,
    MessageType,
    make_message_id,
)
from app.multiagent.policies import TeamRunMode
from app.multiagent.prompts import get_role_prompt
from app.multiagent.review_repair import ReviewRepairLoop, ReviewResult
from app.multiagent.room import TeamRoom
from app.multiagent.runtime_adapter import AgentRuntimeAdapter
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import (
    SharedTeamState,
    TeamArtifactRef,
    TeamDecision,
    TeamIssue,
    TeamPhase,
)
from app.multiagent.store import get_multiagent_store
from app.multiagent.termination import TerminationChecker


class TeamRunner:
    """多智能体团队运行器。"""

    def __init__(
        self,
        task_id: str | None = None,
        room_id: str | None = None,
        store: Any | None = None,
    ):
        self.task_id = task_id
        self.room_id = room_id
        self.store = store or get_multiagent_store()
        self.room: TeamRoom | None = None
        self.adapter: AgentRuntimeAdapter | None = None
        self.selector = SpeakerSelector()
        self.termination_checker: TerminationChecker | None = None
        self.review_loop = ReviewRepairLoop()
        self.run_mode = TeamRunMode.CONTROLLED_GROUP_CHAT
        self.emitter = get_event_emitter()  # SSE 事件总线

        self._round = 0
        self._last_speaker: str | None = None
        self._last_messages: list[AgentMessage] = []
        self._room_messages: list[AgentMessage] = []

    @classmethod
    def create(
        cls,
        goal: str,
        team_name: str = "software_dev_team",
        max_rounds: int = 20,
        review_required: bool = True,
        task_id: str | None = None,
        room_id: str | None = None,
    ) -> "TeamRunner":
        """创建并配置一个新的多 Agent 任务。"""
        # 确保 observability 已初始化（CLI 路径可能没走 main.py lifespan）
        from app.core.observability import init_observability

        init_observability(component="multiagent")
        team_spec = get_team(team_name)
        if team_spec is None:
            msg = f"Team '{team_name}' not found. Available: {_list_teams()}"
            raise ValueError(msg)

        config = TeamRunConfig(
            goal=goal,
            team_name=team_name,
            max_rounds=max_rounds,
            review_required=review_required,
        )

        actual_task_id = task_id or "task_" + uuid.uuid4().hex[:8]
        actual_room_id = room_id or "room_" + uuid.uuid4().hex[:12]

        runner = cls(task_id=actual_task_id, room_id=actual_room_id)
        runner._team_spec = team_spec
        runner.room = TeamRoom.create(
            task_id=actual_task_id,
            config=config,
            team_spec=team_spec,
            store=runner.store,
            room_id=actual_room_id,
        )
        runner.adapter = AgentRuntimeAdapter(
            task_id=actual_task_id,
            room_id=actual_room_id,
        )
        runner.termination_checker = TerminationChecker(
            team_spec=team_spec,
            max_stale_rounds=2,
        )

        logger.info(
            f"TeamRunner created: task={actual_task_id}, room={actual_room_id}, "
            f"team={team_name}, agents={len(team_spec.agents)}"
        )
        return runner

    @classmethod
    def load(cls, room_id: str) -> "TeamRunner | None":
        """从 store 恢复已存在的 team runner。"""
        store = get_multiagent_store()
        meta = store.load_room(room_id)
        if not meta:
            return None
        team_spec = meta["team_spec"]
        config = meta["config"]
        task_id = meta["task_id"]

        runner = cls(task_id=task_id, room_id=room_id, store=store)
        runner._team_spec = team_spec
        runner.room = TeamRoom.load(room_id, store)
        if runner.room is None:
            return None
        runner.room.config = config
        runner.adapter = AgentRuntimeAdapter(task_id=task_id, room_id=room_id)
        runner.termination_checker = TerminationChecker(
            team_spec=team_spec,
            max_stale_rounds=2,
        )
        runner._round = runner.room.state.current_round
        logger.info(f"TeamRunner loaded: task={task_id}, room={room_id}, round={runner._round}")
        return runner

    # ========== 核心循环 ==========

    def run(self, goal_override: str | None = None) -> TeamRunResult:
        """运行多 Agent 团队任务的主循环。

        @traceable 装饰在独立的 _run_team_traced 函数上（见本文件底部），
        确保跨线程 contextvar 传播 + process_inputs/process_outputs 业务内容可见性。
        """
        if not self.room or not self.adapter or not self.termination_checker:
            raise RuntimeError("TeamRunner not initialized. Use TeamRunner.create() or .load() first.")

        start_time = datetime.utcnow()
        seat_agent_names = [a.name for a in self.room.agents]

        # 0. 发送 user_request 到总线
        if goal_override:
            self.room.config.goal = goal_override
        self.room.state.goal = self.room.config.goal
        self.room.state.update_phase(TeamPhase.PLANNING)

        # emit: task started
        self.emitter.emit(
            self.room_id or "",
            "task_started",
            {"goal": self.room.config.goal, "agents": [a.name for a in self.room.agents]},
        )

        # 初始消息
        self.room.send_system_message(
            content=self.room.config.goal,
            message_type=MessageType.USER_REQUEST,
        )
        self._room_messages = self.room.bus.get_room_messages()

        # 1. 主循环
        termination_reason: str | None = None
        while True:
            self._round += 1
            self.room.state.current_round = self._round

            # 本轮 trace span：让 _traced_llm_call + adapter.run 挂为本 run 的 child
            with trace_span(
                "team_round",
                run_type="chain",
                metadata={"round": self._round, "task_id": self.task_id, "room_id": self.room_id},
            ) as round_span:
                phase_before = self.room.state.phase.value
                # 1a. 选择下一发言 Agent
                with trace_span(
                    "select_speaker",
                    run_type="chain",
                    metadata={
                        "candidates": seat_agent_names,
                        "last_speaker": self._last_speaker,
                        "round": self._round,
                    },
                ):
                    speaker = self.selector.select(
                        shared_state=self.room.state,
                        agents=self.room.agents,
                        inbox=self.room.inbox,
                        last_speaker=self._last_speaker,
                        last_message=self._last_messages[-1] if self._last_messages else None,
                    )

                if speaker is None:
                    logger.info(f"[TeamRunner] round {self._round}: no speaker selected, terminating")
                    termination_reason = "no_speaker"
                    self.room.state.update_phase(TeamPhase.FAILED)
                    self.emitter.emit(
                        self.room_id or "",
                        "termination",
                        {"reason": "no_speaker", "round": self._round},
                    )
                    break

                # emit: speaker selected
                self.emitter.emit(
                    self.room_id or "",
                    "speaker_selected",
                    {"agent": speaker.name, "role": speaker.role, "round": self._round},
                )

                # 1b. 加载 inbox + state
                inbox_context = self.room.inbox.get_relevant_context(speaker.name)
                unread = self.room.inbox.list_unread(speaker.name)

                # 1c. 构造 system prompt & 调用运行时
                prompt = self.adapter.build_system_prompt(
                    agent=speaker,
                    shared_state=self.room.state,
                    inbox_context=inbox_context,
                    team_agents=self.room.agents,
                )
                actions = self.adapter.run(
                    agent=speaker,
                    inbox_messages=unread,
                    shared_state=self.room.state,
                )

                self.emitter.emit(
                    self.room_id or "",
                    "actions_emitted",
                    {
                        "agent": speaker.name,
                        "round": self._round,
                        "action_count": len(actions),
                        "action_types": [a.get("type", "?") for a in actions],
                    },
                )

                # 1d. actions 转消息，publish
                produced_messages = AgentRuntimeAdapter.actions_to_messages(
                    agent_name=speaker.name,
                    task_id=self.task_id,
                    room_id=self.room_id,
                    actions=actions,
                    round_number=self._round,
                )
                self._last_messages = []
                for msg in produced_messages:
                    self.room.publish(msg)
                    self._last_messages.append(msg)
                    # emit: 每条消息发布
                    self.emitter.emit(
                        self.room_id or "",
                        "message_published",
                        {
                            "id": msg.id,
                            "from_agent": msg.from_agent,
                            "to_agent": msg.to_agent,
                            "message_type": msg.message_type.value,
                            "content_preview": (msg.content or "")[:200],
                            "round": self._round,
                        },
                    )

                # 1e. 更新 SharedTeamState（处理 update_state / request_review / create_artifact 等 action）
                with trace_span(
                    "process_actions",
                    run_type="chain",
                    metadata={
                        "agent": speaker.name,
                        "action_count": len(actions),
                        "action_types": [a.get("type", "?") for a in actions],
                    },
                ):
                    self._process_actions(speaker.name, actions)
                self.store.save_state(self.room.state)

                # 1f. 标记已读
                for m in unread:
                    self.room.inbox.mark_read(m.id, speaker.name)

                # 1g. 记录 round（包含 LangSmith run URL，便于事后回放）
                msg_ids = [m.id for m in produced_messages]
                action_summary = "; ".join(
                    f"{a.get('type','?')}({'->' + a.get('to_agent','') if a.get('to_agent') else ''})"
                    for a in actions[:5]
                )
                run_url = get_current_run_url()
                self.store.save_round(
                    room_id=self.room_id,
                    round_number=self._round,
                    selected_speaker=speaker.name,
                    action_summary=action_summary[:200],
                    message_ids=msg_ids,
                    langsmith_run_url=run_url,
                )

                # 1h. 检查终止
                self._last_speaker = speaker.name

                # 本轮是否有消息真正到达了某个 Agent 的 inbox？（检测路由黑洞）
                productive_delivery = self._check_productive_delivery(produced_messages)

                with trace_span(
                    "termination_check",
                    run_type="chain",
                    metadata={"round": self._round, "productive_delivery": productive_delivery},
                ):
                    decision = self.termination_checker.check(
                        state=self.room.state,
                        recent_messages=produced_messages,
                        round_count=self._round,
                        productive_delivery=productive_delivery,
                    )

                # 把本轮的业务摘要写到 team_round span 的 metadata，
                # 让 LangSmith UI 上每个 team_round 的 metadata 直接可见轮次业务内容
                round_run = round_span.get("run") if isinstance(round_span, dict) else None
                if round_run is not None and hasattr(round_run, "add_metadata"):
                    try:
                        round_run.add_metadata({
                            "speaker": speaker.name,
                            "speaker_role": speaker.role,
                            "action_types": [a.get("type", "?") for a in actions],
                            "action_summary": action_summary[:200],
                            "produced_messages": [
                                {
                                    "from": m.from_agent,
                                    "to": m.to_agent,
                                    "type": m.message_type.value,
                                    "preview": (m.content or "")[:300],
                                }
                                for m in produced_messages
                            ],
                            "phase_before": phase_before,
                            "phase_after": self.room.state.phase.value,
                            "termination": decision.reason if decision.should_terminate else None,
                        })
                    except Exception:  # noqa: BLE001 - 业务埋点不应影响主流程
                        logger.debug("[TeamRunner] round_run.add_metadata 失败", exc_info=True)

                if decision.should_terminate:
                    termination_reason = decision.reason
                    if decision.final_phase:
                        self.room.state.update_phase(decision.final_phase)
                    self.emitter.emit(
                        self.room_id or "",
                        "termination",
                        {"reason": termination_reason, "round": self._round, "phase": self.room.state.phase.value},
                    )
                    break

        # 2. 完成
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        self.room.state.updated_at = datetime.utcnow()
        self.room.mark_terminated()
        self.store.set_room_terminated(self.room_id, True, self.room.state.phase.value)

        result = TeamRunResult(
            task_id=self.task_id,
            room_id=self.room_id,
            status="completed" if self.room.state.phase in (TeamPhase.COMPLETED, TeamPhase.FINALIZING) else "failed",
            final_output=self.room.state.final_output or "（无最终输出）",
            phase=self.room.state.phase.value,
            total_rounds=self._round,
            termination_reason=termination_reason,
            completed_at=datetime.utcnow(),
        )

        # emit: task terminated
        self.emitter.emit(
            self.room_id or "",
            "task_terminated",
            {
                "status": result.status,
                "phase": result.phase,
                "total_rounds": result.total_rounds,
                "termination_reason": termination_reason,
                "elapsed": round(elapsed, 2),
                "final_output": (result.final_output or "")[:500],
            },
        )

        logger.info(
            f"TeamRunner done: task={self.task_id}, room={self.room_id}, "
            f"rounds={self._round}, reason={termination_reason}, elapsed={elapsed:.1f}s"
        )
        return result

    # ========== Action 处理 ==========

    def _process_actions(self, agent_name: str, actions: list[dict[str, Any]]) -> None:
        """根据 Agent 输出的 actions 更新 SharedTeamState。

        本层做"深度护栏（defense in depth）"：即便 runtime_adapter 在第一层已经
        把越权 action 改为 no_op，这里仍按 agent 的 allowed_actions 再次校验，
        避免任何绕过路径直接落库 / 改状态 / 改 final_output。
        """
        state = self.room.state
        speaking_agent = next(
            (a for a in self.room.agents if a.name == agent_name), None
        )
        allowed_actions = (
            get_effective_allowed_actions(speaking_agent) if speaking_agent else []
        )
        allowed_set = set(allowed_actions) if allowed_actions else None

        for action in actions:
            action_type = action.get("type", "no_op")
            # ---- 深层护栏：未授权 action 不允许触碰 state ----
            if allowed_set is not None and action_type not in allowed_set:
                logger.warning(
                    f"[TeamRunner._process_actions] agent={agent_name} action={action_type} "
                    f"不在白名单 {sorted(allowed_set)}，已拒绝执行状态/消息副作用"
                )
                continue

            if action_type == "update_state":
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
                            f"{s.get('step','')}. {s.get('content',s.get('action',str(s)))}"
                            if isinstance(s, dict) else str(s)
                            for s in plan
                        )
                    else:
                        state.plan = str(plan)

            elif action_type == "create_artifact":
                path = action.get("artifact_path", action.get("content", ""))
                role = action.get("artifact_role", "artifact")
                version = action.get("version", 1)
                artifact_id = action.get("artifact_id")
                if path:
                    self.room.state.add_artifact(
                        TeamArtifactRef(
                            path=path,
                            role=role,
                            produced_by=agent_name,
                            version=version,
                            artifact_id=artifact_id,
                        )
                    )

            elif action_type == "request_review":
                state.review_status = "pending"
                state.update_phase(TeamPhase.REVIEWING)

            elif action_type == "respond_critique":
                pass  # 由 TeamRunner 消息流处理

            elif action_type == "mark_done":
                # 深层防线：只有 Finalizer 角色才允许真正写入 final_output 终止任务
                if speaking_agent is None or speaking_agent.role != "Finalizer":
                    logger.warning(
                        f"[TeamRunner._process_actions] agent={agent_name} "
                        f"role={speaking_agent.role if speaking_agent else 'Unknown'} "
                        f"尝试 mark_done 被深层护栏拒绝（仅 Finalizer 可宣布完成）"
                    )
                    continue
                state.final_output = action.get("content", "")
                state.update_phase(TeamPhase.FINALIZING)

            elif action_type == "handoff":
                to_agent = action.get("to_agent", "")
                if to_agent:
                    state.update_phase(TeamPhase.EXECUTING)

            elif action_type == "send_message":
                # 处理 review_result 类型
                msg_type_str = action.get("message_type", "")
                if msg_type_str == "review_result":
                    # 深层护栏：只有 ReviewerAgent / Reviewer 角色才能产出 review_result
                    if speaking_agent is None or speaking_agent.role not in (
                        "ReviewerAgent",
                        "Reviewer",
                    ):
                        logger.warning(
                            f"[TeamRunner._process_actions] agent={agent_name} "
                            f"role={speaking_agent.role if speaking_agent else 'Unknown'} "
                            f"越权产出 review_result，已拒绝触发返工闭环"
                        )
                        continue
                    raw_msg = action.get("content", "")
                    review_result = ReviewResult(
                        passed=action.get("review_result", {}).get("passed", False),
                        issues=action.get("review_result", {}).get("issues", []),
                        required_fix_owner=action.get("review_result", {}).get("required_fix_owner"),
                        raw=raw_msg,
                    )
                    self.review_loop.process_review_result(
                        result=review_result,
                        state=state,
                        room=self.room,
                    )
                    # P0-2: 同步更新对应 artifact 的 reviewed_by / status
                    artifact_refs = action.get("artifact_refs", []) or []
                    for ref in artifact_refs:
                        path = ref.get("path") if isinstance(ref, dict) else None
                        if path:
                            status = "approved" if review_result.passed else "rejected"
                            state.mark_artifact_reviewed(
                                path=path,
                                reviewed_by=agent_name,
                                status=status,
                                message_id=None,
                            )
                elif msg_type_str == "plan" and state.phase == TeamPhase.PLANNING:
                    # Planner 把计划正式发给 Coder → 进入执行阶段
                    state.update_phase(TeamPhase.EXECUTING)
                elif msg_type_str == "delegation" and state.phase in (TeamPhase.PLANNING, TeamPhase.DISCUSSING):
                    state.update_phase(TeamPhase.EXECUTING)
                elif msg_type_str == "final":
                    # 深层护栏：final 仅 Finalizer 角色有效，其它角色发 final 不写入 final_output
                    if speaking_agent is None or speaking_agent.role != "Finalizer":
                        logger.warning(
                            f"[TeamRunner._process_actions] agent={agent_name} "
                            f"role={speaking_agent.role if speaking_agent else 'Unknown'} "
                            f"越权发 final 消息，不写入 final_output（仅 Finalizer 可）"
                        )
                        continue
                    state.final_output = action.get("content", "") or state.final_output
                    state.update_phase(TeamPhase.FINALIZING)

    # ========== 生产性投递检测 ==========

    def _check_productive_delivery(self, produced_messages: list[AgentMessage]) -> bool:
        """判断本轮产出的消息是否有任何一条真正到达了某个真实 Agent 的 inbox。

        判断逻辑：对每条非 no_op 消息，检查其 to_agent（若存在）或订阅匹配的 Agent
        至少有一个不是发言者本人。若 to_agent 是幻觉名字被回退到 broadcast，
        也算 productive（订阅者真实存在）。

        Returns:
            True 表示本轮有有效投递；False 表示本轮消息全部进入路由黑洞或全是 no_op。
        """
        if not produced_messages:
            return False

        agent_names = {a.name for a in self.room.agents}
        for msg in produced_messages:
            if msg.message_type == MessageType.NO_OP:
                continue
            # 更新状态/创建 artifact 算有效推进
            if msg.message_type in (MessageType.STATE_UPDATE, MessageType.ARTIFACT_CREATED):
                return True
            # 有显式 to_agent：检查是否是真实 agent（含 routing_fallback 回退也算）
            if isinstance(msg.to_agent, str) and msg.to_agent:
                if msg.to_agent in agent_names:
                    return True
                # routing_fallback=True 表示 bus 已把这条消息转 broadcast
                if (msg.metadata or {}).get("routing_fallback"):
                    return True
                # 未知 agent 且无 fallback 标记：不算
                continue
            # 无 to_agent 的 broadcast：检查订阅者中是否有非发言人
            subs = self.room.bus.get_subscriptions_for_message(msg) if hasattr(self.room.bus, "get_subscriptions_for_message") else None
            if subs is None:
                # 退而求其次：广播+无 to_agent 默认算有效（订阅系统会处理）
                return True
            for sub_name in subs:
                if sub_name != msg.from_agent and sub_name in agent_names:
                    return True
        return False

    # ========== 快速辅助 ==========

    def get_room_state(self) -> SharedTeamState:
        return self.room.state if self.room else None

    def get_messages(self, limit: int = 200) -> list[AgentMessage]:
        if self.room:
            return self.store.get_room_messages(self.room_id, limit)
        return []

    def get_agents(self) -> list[AgentSpec]:
        return self.room.agents if self.room else []

    def get_rounds(self) -> list[dict[str, Any]]:
        return self.store.list_rounds(self.room_id)

    def cancel(self) -> bool:
        if not self.room:
            return False
        self.room.request_cancel()
        self.room.state.metadata["cancel_requested"] = True
        self.room.state.update_phase(TeamPhase.CANCELLED)
        self.store.save_state(self.room.state)
        self.store.set_room_terminated(self.room_id, True, "cancelled")
        return True


def run_team_task(
    goal: str,
    team_name: str = "software_dev_team",
    max_rounds: int = 20,
    review_required: bool = True,
    task_id: str | None = None,
) -> TeamRunResult:
    """快速运行多 Agent 任务的便利函数。"""
    runner = TeamRunner.create(
        goal=goal,
        team_name=team_name,
        max_rounds=max_rounds,
        review_required=review_required,
        task_id=task_id,
    )
    return _run_team_traced(runner)


def _extract_team_run_inputs(args: tuple) -> dict[str, Any]:
    """@traceable 的 process_inputs：args[0] 是 TeamRunner 实例。

    从中提取 goal / team_name / max_rounds / agents，让 LangSmith UI 上 team_run 的
    输入是结构化字段而非 Python 对象字符串。
    """
    runner = args[0] if args else None
    if runner is None or not isinstance(runner, TeamRunner):
        return {"goal": "?"}
    cfg = getattr(runner.room, "config", None) if runner.room else None
    agents = [a.name for a in runner.room.agents] if runner.room else []
    return {
        "goal": getattr(cfg, "goal", "?"),
        "team_name": getattr(cfg, "team_name", "?"),
        "max_rounds": getattr(cfg, "max_rounds", 0),
        "agents": agents,
        "task_id": runner.task_id,
        "room_id": runner.room_id,
    }


def _extract_team_run_outputs(result: Any) -> dict[str, Any]:
    """@traceable 的 process_outputs：把 TeamRunResult 结构化为简洁字段。"""
    if not isinstance(result, TeamRunResult):
        return {"result": str(result)[:200]}
    return {
        "status": result.status,
        "phase": result.phase,
        "total_rounds": result.total_rounds,
        "termination_reason": result.termination_reason,
        "final_output": (result.final_output or "")[:500],
        "task_id": result.task_id,
        "room_id": result.room_id,
    }


@traceable(
    name="team_run",
    run_type="chain",
    process_inputs=_extract_team_run_inputs,
    process_outputs=_extract_team_run_outputs,
    tags=["multiagent", "team_run"],
)
def _run_team_traced(runner: "TeamRunner", goal_override: str | None = None) -> TeamRunResult:
    """被 @traceable 装饰的入口函数，包装 TeamRunner.run()。

    设计要点：
    1. **顶层 run 由 @traceable 在调用线程创建**，依赖 langsmith contextvar 继承。
       routes_team.py 用 contextvars.copy_context() 启动后台线程，确保 trace 父子链正确建立。
    2. **process_inputs/process_outputs 让 LangSmith UI 上看到结构化业务内容**
       （goal/agents/total_rounds/phase/final_output），而非 Python 对象字符串。
    3. CLI 直接调用 runner.run() 时不会包 this wrapper；CLI 路径也应改用此函数以获得 trace。
    """
    return runner.run(goal_override)
