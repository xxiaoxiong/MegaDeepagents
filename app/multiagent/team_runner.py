"""TeamRunner：多智能体团队运行核心循环（复用 TeamRoundExecutor）。

核心流程：
1. create room（或 load 已有 room）
2. 初始化 TeamRoundExecutor（统一封装选择 Agent、加载 Inbox、调用 Agent、
   发布 MessageBus、更新 SharedTeamState、持久化、termination）
3. publish user_request 到总线
4. loop: execute_round → 判断终止
5. finalize

注意：
- Agent 通过 AgentRuntimeAdapter 调用真实 LLM（build_model），产出 JSON actions。
- 每轮完整链路：select_speaker → adapter.run → actions_to_messages → bus.publish →
  process_actions(state update) → persist_round → termination_check。
- ReviewRepairLoop 已接入主链路：review_result 触发 critique 消息发布到 MessageBus。
- 与 TeamGraph 共享 TeamRoundExecutor 组件，不再复制两套业务逻辑。
- TeamRoundExecutor 作为共享单轮执行组件，供 TeamRunner.run() 和 TeamGraphRunner 共同使用。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.core.observability import traceable, trace_span, get_current_run_url
from app.multiagent.agent_spec import (
    AgentSpec,
    TeamRunConfig,
    TeamRunResult,
    TeamSpec,
)
from app.multiagent.default_teams import get_team, list_teams as _list_teams
from app.multiagent.event_emitter import get_event_emitter
from app.multiagent.messages import AgentMessage, MessageType
from app.multiagent.policies import EffectiveRunPolicy, TeamRunMode
from app.multiagent.review_repair import ReviewRepairLoop
from app.multiagent.room import TeamRoom
from app.multiagent.runtime_adapter import AgentRuntimeAdapter
from app.multiagent.round_executor import TeamRoundExecutor
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import (
    SharedTeamState,
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
        self.round_executor: TeamRoundExecutor | None = None
        self.run_mode = TeamRunMode.CONTROLLED_GROUP_CHAT
        self.emitter = get_event_emitter()  # SSE 事件总线

        self._round = 0
        self._last_speaker: str | None = None
        self._last_messages: list[AgentMessage] = []
        self._room_messages: list[AgentMessage] = []
        self._team_spec: TeamSpec | None = None
        self._effective_policy: EffectiveRunPolicy | None = None

    def _init_executor(self) -> None:
        """懒初始化 TeamRoundExecutor。"""
        if self.round_executor is None and self.room is not None:
            self.round_executor = TeamRoundExecutor(
                room=self.room,
                adapter=self.adapter,
                selector=self.selector,
                termination_checker=self.termination_checker,
                review_loop=self.review_loop,
                store=self.store,
                emitter=self.emitter,
                task_id=self.task_id,
                room_id=self.room_id,
                team_spec=self._team_spec,
            )

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

        # 计算有效运行策略，确保 review_required / max_rounds 在所有组件中一致
        effective_policy = EffectiveRunPolicy.from_team_and_run_config(team_spec, config)

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
        # 使用有效策略创建 TerminationChecker，而非直接从 team_spec 读取
        runner.termination_checker = TerminationChecker(
            team_spec=team_spec,
            max_stale_rounds=2,
            review_required=effective_policy.review_required,
        )
        # ReviewRepairLoop 也使用有效策略的 max_review_cycles
        runner.review_loop.reset_max_cycles(effective_policy.max_review_cycles)
        # 将有效策略存入 runner，便于其他组件查询
        runner._effective_policy = effective_policy

        logger.info(
            f"TeamRunner created: task={actual_task_id}, room={actual_room_id}, "
            f"team={team_name}, agents={len(team_spec.agents)}, "
            f"review_required={effective_policy.review_required}"
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
        # 在构造依赖组件之前先计算有效策略
        runner._effective_policy = EffectiveRunPolicy.from_team_and_run_config(team_spec, config)
        runner.adapter = AgentRuntimeAdapter(task_id=task_id, room_id=room_id)
        runner.termination_checker = TerminationChecker(
            team_spec=team_spec,
            max_stale_rounds=2,
            review_required=runner._effective_policy.review_required,
        )
        runner.review_loop.reset_max_cycles(runner._effective_policy.max_review_cycles)
        runner._round = runner.room.state.current_round
        logger.info(f"TeamRunner loaded: task={task_id}, room={room_id}, round={runner._round}")
        return runner

    @property
    def effective_policy(self) -> EffectiveRunPolicy:
        """返回当前生效的运行策略，用于查询 review_required / max_rounds 真实值。"""
        if self._effective_policy is None:
            # 临时构造：从 team_spec 和当前 room.config 推断
            cfg = self.room.config if self.room else None
            self._effective_policy = EffectiveRunPolicy.from_team_and_run_config(
                self._team_spec, cfg,
            )
        return self._effective_policy

    # ========== 核心循环 ==========

    def run(self, goal_override: str | None = None) -> TeamRunResult:
        """运行多 Agent 团队任务的主循环。

        全部复用 TeamRoundExecutor 完成单轮执行逻辑。
        """
        if not self.room or not self.adapter or not self.termination_checker:
            raise RuntimeError("TeamRunner not initialized. Use TeamRunner.create() or .load() first.")

        start_time = datetime.utcnow()
        self._init_executor()

        # 0. 发送 user_request 到总线
        if goal_override:
            self.room.config.goal = goal_override
        self.room.state.goal = self.room.config.goal
        self.room.state.update_phase(TeamPhase.PLANNING)

        self.emitter.emit(
            self.room_id or "",
            "task_started",
            {"goal": self.room.config.goal, "agents": [a.name for a in self.room.agents]},
        )

        self.room.send_system_message(
            content=self.room.config.goal,
            message_type=MessageType.USER_REQUEST,
        )
        self._room_messages = self.room.bus.get_room_messages()

        # 1. 主循环
        termination_reason: str | None = None
        while True:
            # === 取消检查：每轮开始前从持久化状态读取 cancel request ===
            # 取消信号写过的话立刻停止，不再执行后续节点
            if self.room.is_cancel_requested():
                logger.info(f"[TeamRunner] cancel detected at round {self._round}, stopping")
                self.room.state.update_phase(TeamPhase.CANCELLED)
                termination_reason = "cancel_requested"
                self.emitter.emit(
                    self.room_id or "", "termination",
                    {"reason": "cancel_requested", "round": self._round, "phase": TeamPhase.CANCELLED.value},
                )
                break
            self._round += 1
            self.room.state.current_round = self._round

            with trace_span(
                "team_round",
                run_type="chain",
                metadata={"round": self._round, "task_id": self.task_id, "room_id": self.room_id},
            ) as round_span:
                phase_before = self.room.state.phase.value

                # 使用 TeamRoundExecutor 执行一轮
                result = self.round_executor.execute_round(
                    round_number=self._round,
                    last_speaker=self._last_speaker,
                    last_messages=self._last_messages,
                )

                if result.error:
                    logger.error(f"[TeamRunner] round {self._round} error: {result.error}")
                    termination_reason = result.error
                    self.room.state.update_phase(TeamPhase.FAILED)
                    self.emitter.emit(
                        self.room_id or "", "termination",
                        {"reason": result.error, "round": self._round},
                    )
                    break

                # 更新追踪状态
                self._last_speaker = result.speaker.name if result.speaker else self._last_speaker
                self._last_messages = result.produced_messages

                # trace span metadata
                if result.speaker:
                    action_summary = "; ".join(
                        f"{a.get('type', '?')}({'->' + a.get('to_agent', '') if a.get('to_agent') else ''})"
                        for a in result.actions[:5]
                    )
                    round_run = round_span.get("run") if isinstance(round_span, dict) else None
                    if round_run is not None and hasattr(round_run, "add_metadata"):
                        try:
                            round_run.add_metadata({
                                "speaker": result.speaker.name,
                                "speaker_role": result.speaker.role,
                                "action_types": [a.get("type", "?") for a in result.actions],
                                "action_summary": action_summary[:200],
                                "produced_messages": [
                                    {"from": m.from_agent, "to": m.to_agent, "type": m.message_type.value, "preview": (m.content or "")[:300]}
                                    for m in result.produced_messages
                                ],
                                "phase_before": phase_before,
                                "phase_after": self.room.state.phase.value,
                                "termination": result.termination_reason,
                            })
                        except Exception:
                            logger.debug("[TeamRunner] round_run.add_metadata 失败", exc_info=True)

                if result.should_terminate:
                    termination_reason = result.termination_reason
                    # 归一化：若最终 phase 是 CANCELLED，统一 termination_reason 为 cancel_requested
                    #（避免 execute_round 期间 cancel() 触发后 TerminationChecker 返回
                    # phase_already_cancelled 导致语义分裂）
                    if self.room.state.phase == TeamPhase.CANCELLED:
                        termination_reason = "cancel_requested"
                    self.emitter.emit(
                        self.room_id or "", "termination",
                        {"reason": termination_reason, "round": self._round, "phase": self.room.state.phase.value},
                    )
                    break

        # 2. 完成
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        self.room.state.updated_at = datetime.utcnow()
        self.room.mark_terminated()
        self.store.set_room_terminated(self.room_id, True, self.room.state.phase.value)

        # 终态 → status 映射：仅 COMPLETED 视为成功；INCOMPLETE 仍是 failed
        # 但通过 termination_reason 区分细节
        if self.room.state.phase == TeamPhase.COMPLETED:
            result_status = "completed"
        elif self.room.state.phase == TeamPhase.CANCELLED:
            result_status = "cancelled"
        elif self.room.state.phase == TeamPhase.INCOMPLETE:
            result_status = "failed"  # 达到 max_rounds 但未完成
        elif self.room.state.phase == TeamPhase.FAILED:
            result_status = "failed"
        else:
            # 非终态被强制收尾（异常路径）→ failed
            result_status = "failed"

        result = TeamRunResult(
            task_id=self.task_id,
            room_id=self.room_id,
            status=result_status,
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
        """转发到 TeamRoundExecutor._process_actions。

        历史背景：早期 TeamRunner 自带一份 _process_actions 实现，包含了与 TeamGraph
        相同的深层护栏逻辑。Req 3 抽取 TeamRoundExecutor 后此方法已不再被主循环使用
        （run() 走 round_executor.execute_round()）。保留为转发壳是为了兼容任何外部
        调用与测试代码，避免再次分裂出两套业务逻辑。
        """
        # 仅初始化 executor，然后转交归一化实现，**禁止再恢复一套本地护栏副本**。
        if self.round_executor is None:
            self._init_executor()
        # 保护：若任何依赖未就绪（如 load 路径上游漏初始化），不静默吞错。
        if self.round_executor is None or self.room is None:
            logger.warning(
                f"[TeamRunner._process_actions] called but executor/room not ready"
            )
            return
        speaker = next((a for a in self.room.agents if a.name == agent_name), None)
        if speaker is None:
            logger.warning(
                f"[TeamRunner._process_actions] agent={agent_name} not found in room"
            )
            return
        self.round_executor._process_actions(speaker, actions)

    # ========== B3: Agent 跨任务持久记忆 ==========

    def _persist_agent_memory(
        self,
        agent: AgentSpec,
        actions: list[dict[str, Any]],
        messages: list[AgentMessage],
    ) -> None:
        """把本轮 Agent 的可学项沉淀到 LayeredMemorySystem（semantic + procedural）。

        策略（保守、避免噪声）：
        - create_artifact / request_review / mark_done 等关键决策写一条 procedural
          "经验"短句；其后该 Agent 再次遇到类似 goal 时可在 prompt 中回顾。
        - send_message 含技术决策（content 较长且非 no_op）→ 写一条 semantic 知识。
        - 重复内容用 content hash 做幂等去重，importance 累加。
        所有写入受 try/except 保护，记忆失败不阻断主循环。
        """
        if not agent or not actions:
            return
        scope = agent.private_memory_scope or agent.name
        try:
            from app.multiagent.layered_memory import (
                get_layered_memory, MemoryTier, PersistentMemory,
            )
            memory = get_layered_memory()
        except Exception as exc:
            logger.debug(f"[B3] load layered memory 失败，跳过持久化：{exc}")
            return

        importance_bump = 0.05
        for action in actions:
            try:
                atype = action.get("type", "no_op")
                content = (action.get("content") or "").strip()
                # 1) procedural：关键操作产出 SOP 短句
                if atype in ("create_artifact", "request_review", "respond_critique", "mark_done"):
                    target = action.get("to_agent") or action.get("artifact_role") or "?"
                    summary = (
                        f"[{atype}] -> {target}: "
                        f"{content[:160] or '(无内容)'}"
                    )
                    entry_id = f"proc_{scope}_{hash(summary) & 0xFFFFFFFF:x}"
                    existing = memory.procedural.get(entry_id)
                    if existing is None:
                        memory.add(
                            MemoryTier.PROCEDURAL,
                            content=summary,
                            agent_scope=scope,
                            importance=0.6,
                            metadata={"source_action": atype, "id": entry_id, "task_id": self.task_id},
                            task_id=self.task_id,
                        )
                    else:
                        existing.importance = min(1.0, existing.importance + importance_bump)
                        memory.procedural._persist(existing, task_id=self.task_id)
                # 2) semantic：技术性 send_message 当作知识沉淀
                elif atype == "send_message" and len(content) >= 30:
                    summary = f"[send_to:{action.get('to_agent','?')}] {content[:200]}"
                    entry_id = f"sem_{scope}_{hash(summary) & 0xFFFFFFFF:x}"
                    existing = memory.semantic.get(entry_id)
                    if existing is None:
                        memory.add(
                            MemoryTier.SEMANTIC,
                            content=summary,
                            agent_scope=scope,
                            importance=0.5,
                            metadata={"source_action": atype, "id": entry_id, "task_id": self.task_id},
                            task_id=self.task_id,
                        )
                    else:
                        existing.importance = min(1.0, existing.importance + importance_bump)
                        memory.semantic._persist(existing, task_id=self.task_id)
            except Exception as exc:
                logger.debug(f"[B3] persist agent memory for {agent.name} action={action.get('type')} 失败：{exc}")

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
