"""LangGraph team graph：把 TeamRunner 的主循环表达成可 checkpoint 的状态图。

设计目标（P4-1）：
1. 把 TeamRunner 的 4 个核心步骤（speaker_select → run_speaker → process → decide_terminate）
   映射成 LangGraph StateGraph 的 4 个节点 + 边
2. 整图作为"团队运行"的可恢复执行单元：在任意节点后可 SqliteSaver checkpoint
3. team_task_id 用作 thread_id：同一团队任务的多次启动可恢复到上一次中断点
4. 与现有 TeamRunner.run() 并存：原同步主循环继续作为简单路径使用；
   -run_via_graph() 走 LangGraph 执行模型，可中断 / 可恢复 / 可观测

注意（Req 10）：
- 本模块是**实验性**组件，**未接入 API 与 CLI**
- node_run_speaker 内部通过 TeamRoundExecutor.execute_round() 与 TeamRunner.run()
  共享同一套单轮业务逻辑（Req 3 → 与 Test req 9 等价性验证确认）
- checkpoint 恢复逻辑仍需更多实机验证；Node-level round 递增 / 传播通过 dict reducer
  全量传递，后续若改用 Annotated reducer（operator.add）需注意 round 不重复累加
- HITL 中断节点（node_hitl_wait）使用了 langgraph.types.interrupt，但尚未与
  HITL 端点对接——属于"结构预留"而非已生效的主路径

失败容忍策略：
- LangGraph 不可用（缺包等）→ 退化为同步主循环（已有 TeamRunner.run）
- Checkpoint 写入失败 → 不阻塞业务运行，记录 WARNING
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.state import TeamPhase

try:  # pragma: no cover - 由环境决定
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import interrupt  # noqa: F401  保留供 HITL 用
    _LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover
    StateGraph = None  # type: ignore
    END = None  # type: ignore
    SqliteSaver = None  # type: ignore
    _LANGGRAPH_AVAILABLE = False


# ===== Graph State Schema =====
# 用一个轻量 dict 表达 graph 状态，与 SharedTeamState 解耦：
# - round：当前轮次
# - phase：当前阶段（来自 SharedTeamState.phase.value）
# - speaker：本轮选中的 Agent 名
# - actions：本轮 speaker 产出的 actions 列表
# - messages：本轮 publish 的消息 ID 列表
# - termination_reason：终止原因（None = 继续运行）
# - error：异常（若有）
# - hitl_pending：是否有未决 HITL 冲突（True 时图在中断节点等待人工决议）
GraphState = dict[str, Any]


class TeamGraphRunner:
    """LangGraph 模式下的 TeamRunner。

    使用方式：
        gr = TeamGraphRunner(team_runner)
        gr.compile(checkpoint_path="team_checkpoints.sqlite")
        gr.run_via_graph(goal="...", resume_thread_id=room_id)
    """

    def __init__(self, team_runner: Any) -> None:
        self.runner = team_runner
        self.graph = None
        self.checkpointer = None
        self._lock = threading.Lock()

    # ===== 节点定义 =====
    # 每个节点接收 GraphState，返回 GraphState 增量；TeamRunner 私有方法承担业务逻辑。

    def node_select_speaker(self, state: GraphState) -> GraphState:
        """节点 1：递增轮次计数。

        本节点不再调用 SpeakerSelector.select——因为 node_run_speaker 内部
        已通过 TeamRoundExecutor.execute_round() 完成 select + run + publish +
        process_actions + termination 整条链路（与 TeamRunner.run() 主循环一致）。
        若在此处再次 select，会导致选择冲突（double select），破坏与 TeamRunner
        的等价性（Test req 9）。

        轮次在此递增后传给 execute_round，保证 checkpoint 中保存正确轮次。
        """
        try:
            # 每轮递增一次（保证 checkpoint 中保存正确轮次）
            _round = state.get("round", 0) + 1

            if not self.runner.room or not getattr(self.runner, "selector", None):
                return {
                    "speaker": None,
                    "round": _round,
                    "termination_reason": "no_speaker",
                    "hitl_pending": False,
                }

            # 若 room.state 不可用（测试 mock 路径），返回空 speaker
            if self.runner.room.state is None:
                return {
                    "speaker": None,
                    "round": _round,
                    "termination_reason": "no_state",
                    "hitl_pending": False,
                }

            self.runner.room.state.current_round = _round

            # 不选 speaker——execute_round 内部会选
            # 注意：必须传播 last_speaker / last_messages，否则 dict reducer 会擦除它们
            return {
                "speaker": None,  # 让 execute_round 自己选
                "round": _round,
                "last_speaker": state.get("last_speaker"),
                "last_messages": state.get("last_messages", []),
                "hitl_pending": False,
                "termination_reason": None,
            }
        except Exception as exc:
            logger.error(f"[TeamGraph] node_select_speaker failed: {exc}")
            return {"error": str(exc), "termination_reason": str(exc), "round": state.get("round", 0) + 1}

    def node_run_speaker(self, state: GraphState) -> GraphState:
        """节点 2：完整执行本轮（通过 execute_round）。

        Req 3 等价性约束：调用 TeamRoundExecutor.execute_round()，与
        TeamRunner.run() 主循环走**同一份**单轮业务逻辑。

        last_speaker 传递策略：
        - graph 状态 dict 中的 round 已通过所有显式返回修复保留，但 last_speaker
          仍会被 LangGraph 默认 dict reducer 擦除（部分节点不返回此键）。
        - 故优先使用 state.get("last_speaker")，若无则回退到 runner._last_speaker
          （主循环持久持有的跟踪变量），确保 selector 看到正确的历史发言人。
        """
        _round = state.get("round", 0)
        if not self.runner.room or not self.runner.adapter:
            return {"actions": [], "round": _round, "termination_reason": "no_room_or_adapter"}
        try:
            if not self.runner.round_executor:
                self.runner._init_executor()
            if self.runner.round_executor is None:
                return {"actions": [], "round": _round, "termination_reason": "no_executor"}

            # 优先 state 中的历史，若无则回退 runner._last_speaker（主循环同步值）
            effective_last_speaker = state.get("last_speaker") or self.runner._last_speaker
            effective_last_messages = state.get("last_messages") or self.runner._last_messages or []

            result = self.runner.round_executor.execute_round(
                round_number=_round,
                last_speaker=effective_last_speaker,
                last_messages=effective_last_messages,
                cancel_check=True,
            )

            # 同步 TeamRunner 主循环持有的跟踪状态，供下一轮选择使用
            self.runner._last_speaker = (result.speaker.name if result.speaker
                                         else effective_last_speaker)
            self.runner._last_messages = result.produced_messages

            return {
                "actions": result.actions,
                "messages": result.produced_messages,
                "round": _round,
                "last_speaker": self.runner._last_speaker,
                "last_messages": result.produced_messages,
                "termination_reason": result.termination_reason,
                "should_terminate": result.should_terminate,
            }
        except Exception as exc:
            logger.error(f"[TeamGraph] node_run_speaker failed: {exc}")
            return {"error": str(exc), "round": _round}

    def node_process_actions(self, state: GraphState) -> GraphState:
        """节点 3：no-op passthrough，但传播 round/messages。

        历史背景：早期 graph 自带一套 _process_actions 副本"做深层护栏 + 持久化"。
        Req 3 抽 TeamRoundExecutor 后这一步已由 node_run_speaker 内部的
        execute_round() 完成（_process_actions / save_state / 持久化轮次记录）。
        若本节点再次调用 _process_actions，会造成状态被双重处理 → 详情计数翻倍、
        review_result 闭环被重复触发，是等价性破裂的直接根因。

        保留节点壳是为了 graph 拓扑可读性（select → run → process → decide），
        但本节点不再产生副作用。

        注意（LangGraph dict reducer）：StateGraph(dict) 默认按节点输出替换整状态，
        故本节点必须显式传播 round / messages，否则后续节点丢失之。
        """
        _round = state.get("round", 0)
        return {"round": _round, "messages": state.get("messages", [])}

    def node_decide_terminate(self, state: GraphState) -> GraphState:
        """节点 4：终止判断。返回 {'continue': True/False, 'termination_reason': ...}。

        优先采用 execute_round 已得到的 should_terminate（节点 2 内已调用
        TerminationChecker），避免图再次"独立判断一次"导致与主循环语义分裂。
        若 run 节点未给出明确终止信号，再 fallback 到一次 TerminationChecker.check，
        并补上 max_rounds 上限保护。
        """
        try:
            # 1. 优先尊重 executor 的判断（与 TeamRunner.run() 主循环等价）
            if state.get("should_terminate"):
                reason = state.get("termination_reason") or "terminated"
                return {
                    "continue": False,
                    "hitl_pending": False,
                    "termination_reason": reason,
                    "round": state.get("round", 0),
                    "messages": state.get("messages", []) or [],
                }

            # 2. 否则补一次独立判断（用于非典型路径，如 max_rounds）
            decision = self.runner.termination_checker.check(
                state=self.runner.room.state,
                recent_messages=state.get("messages", []) or [],
                round_count=state.get("round", 0),
            )
            should_continue = (
                not decision.should_terminate
                and state.get("round", 0) < self.runner.room.state.max_rounds
            )
            return {
                "continue": should_continue,
                "hitl_pending": False,
                "termination_reason": decision.reason if decision.should_terminate else None,
                "round": state.get("round", 0),
                "messages": state.get("messages", []) or [],
            }
        except Exception as exc:
            logger.error(f"[TeamGraph] node_decide_terminate failed: {exc}")
            return {"continue": False, "termination_reason": str(exc),
                    "round": state.get("round", 0)}

    def node_hitl_wait(self, state: GraphState) -> GraphState:
        """可选节点：HITL 等待。若 state.hitl_pending=True 则 interrupt；否则直通。

        通过 langgraph.types.interrupt 实现暂停，等待 resume 时人工输入 resolution。
        """
        # 默认不阻塞；只有显式 hitl_pending=True 才进入中断
        if state.get("hitl_pending"):
            logger.info("[TeamGraph] 进入 HITL 中断节点，等待人工决议")
            # 实际使用：从 HITL API 端点 resume，传 resolution 进来
            if _LANGGRAPH_AVAILABLE:
                try:
                    resolution = interrupt("HITL_REQUIRED")  # type: ignore
                    return {"hitl_resolution": resolution, "hitl_pending": False}
                except Exception:
                    pass
        return {"hitl_pending": False}

    # ===== 编译 / 运行 =====
    def compile(self, checkpoint_path: str | None = None) -> None:
        """编译 graph；提供 checkpoint_path 则启用 SqliteSaver。

        若 LangGraph 不可用，不抛错，仅记 WARNING——调用方应使用 .run_via_graph() 兜底。
        """
        if not _LANGGRAPH_AVAILABLE:
            logger.warning("[TeamGraph] langgraph 不可用，graph 模式关闭")
            self.graph = None
            self.checkpointer = None
            return
        builder = StateGraph(dict)
        builder.add_node("select_speaker", self.node_select_speaker)
        builder.add_node("run_speaker", self.node_run_speaker)
        builder.add_node("process_actions", self.node_process_actions)
        builder.add_node("decide_terminate", self.node_decide_terminate)
        builder.add_node("hitl_wait", self.node_hitl_wait)

        builder.set_entry_point("select_speaker")
        builder.add_edge("select_speaker", "run_speaker")
        builder.add_edge("run_speaker", "process_actions")
        builder.add_edge("process_actions", "decide_terminate")
        # 条件边：未终止 → 回 select_speaker；终止 → END；HITL pending → hitl_wait
        builder.add_conditional_edges(
            "decide_terminate",
            lambda s: "hitl_wait" if s.get("hitl_pending") else ("continue" if s.get("continue") else "end"),
            {
                "continue": "select_speaker",
                "hitl_wait": "hitl_wait",
                "end": END,
            },
        )
        builder.add_edge("hitl_wait", "select_speaker")  # HITL 解决后回到 speak

        # Checkpoint
        if checkpoint_path:
            try:
                import sqlite3
                conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
                self.checkpointer = SqliteSaver(conn)
            except Exception as exc:
                logger.warning(f"[TeamGraph] SqliteSaver 构造失败，回退无 checkpoint：{exc}")
                self.checkpointer = None

        if self.checkpointer:
            self.graph = builder.compile(checkpointer=self.checkpointer)
        else:
            self.graph = builder.compile()

    def run_via_graph(
        self,
        goal: str | None = None,
        resume_thread_id: str | None = None,
        max_rounds_cap: int = 50,
    ) -> dict[str, Any]:
        """通过 LangGraph 执行团队运行；若无 graph 则回退到 TeamRunner.run()。

        Args:
            goal: 任务目标（首次运行必填）
            resume_thread_id: 恢复到指定 thread_id 的最近 checkpoint
            max_rounds_cap: 安全上限，防止图死循环

        Returns:
            {"status": "completed" | "failed" | "interrupted",
             "thread_id": room_id, "rounds": n, "final_output": "..."}
        """
        if self.graph is None:
            logger.info("[TeamGraph] graph 未编译或 langgraph 不可用，回退到同步主循环")
            try:
                self.runner.run(goal_override=goal)
            except Exception as exc:
                logger.error(f"[TeamGraph] fallback run failed: {exc}")
            state_obj = getattr(self.runner.room, "state", None) if self.runner.room else None
            # 根据终止原因确定准确状态
            status = "completed"
            if state_obj:
                if state_obj.phase == TeamPhase.INCOMPLETE:
                    status = "failed"
                elif state_obj.phase == TeamPhase.FAILED:
                    status = "failed"
                elif state_obj.phase == TeamPhase.CANCELLED:
                    status = "cancelled"
            return {
                "status": status,
                "thread_id": resume_thread_id or self.runner.room_id,
                "rounds": state_obj.current_round if state_obj else 0,
                "final_output": state_obj.final_output if state_obj else None,
            }

        thread_id = resume_thread_id or self.runner.room_id
        initial_state: GraphState = {
            "round": 0,
            "speaker": None,
            "actions": [],
            "messages": [],
            "termination_reason": None,
            "hitl_pending": False,
        }

        # 让 TeamRunner 先进入主循环前置（构造 room / 发初始 user_request / 进入 PLANNING）
        # 我们假定调用方已先调用 runner.run 的准备工作（或Compile 不重头做）
        # 这里直接 invoke graph
        # 设定合理的递归上限：每轮 4 个节点 + 安全裕量
        recursion_limit = max(max_rounds_cap * 5 + 10, 50)
        try:
            final = self.graph.invoke(
                initial_state,
                config={
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": recursion_limit,
                },
            )
            # 安全上限自检
            rounds = final.get("round", 0)
            if rounds > max_rounds_cap:
                logger.warning(f"[TeamGraph] 达到安全上限 {max_rounds_cap}，强制停止")

            status = "interrupted" if final.get("hitl_pending") else "completed"
            term_reason = final.get("termination_reason")
            # 归一化：execute_round 期间触发 cancel() 后 TerminationChecker 可能返回
            # phase_already_cancelled，统一回退到 cancel_requested 以保持与 TeamRunner
            # 主循环一致的语义（避免两个路径 cancellation 字符串分裂）。
            state_obj_pre = getattr(self.runner.room, "state", None) if self.runner.room else None
            if state_obj_pre and state_obj_pre.phase == TeamPhase.CANCELLED:
                if term_reason in (None, "phase_already_cancelled"):
                    term_reason = "cancel_requested"

            if term_reason == "no_speaker":
                status = "failed"
            elif term_reason == "cancel_requested":
                status = "cancelled"

            state_obj = state_obj_pre
            # 优先以 SharedTeamState.phase 为准（cancel 后已置 CANCELLED）
            if state_obj and state_obj.phase == TeamPhase.CANCELLED:
                status = "cancelled"
            elif state_obj and state_obj.phase == TeamPhase.INCOMPLETE:
                status = "failed"
            elif state_obj and state_obj.phase == TeamPhase.FAILED:
                status = "failed"

            state_obj = getattr(self.runner.room, "state", None) if self.runner.room else None
            return {
                "status": status,
                "thread_id": thread_id,
                "rounds": rounds,
                "final_output": state_obj.final_output if state_obj else None,
                "termination_reason": term_reason,
                "hitl_pending": final.get("hitl_pending", False),
            }
        except Exception as exc:
            logger.error(f"[TeamGraph] run_via_graph 失败：{exc}")
            return {
                "status": "failed",
                "thread_id": thread_id,
                "rounds": 0,
                "error": str(exc),
            }


def build_team_graph(
    team_runner: Any,
    checkpoint_path: str | None = None,
) -> TeamGraphRunner:
    """工厂函数：构造 + 编译 TeamGraphRunner。

    Args:
        team_runner: 已 create() 但未 run() 的 TeamRunner 实例
        checkpoint_path: checkpoint sqlite 文件路径；None 则无 checkpoint
    """
    gr = TeamGraphRunner(team_runner)
    gr.compile(checkpoint_path=checkpoint_path)
    return gr
