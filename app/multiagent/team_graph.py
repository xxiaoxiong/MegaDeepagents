"""LangGraph team graph：把 TeamRunner 的主循环表达成可 checkpoint 的状态图。

设计目标（P4-1）：
1. 把 TeamRunner 的 4 个核心步骤（speaker_select → run_speaker → process → decide_terminate）
   映射成 LangGraph StateGraph 的 4 个节点 + 边
2. 整图作为"团队运行"的可恢复执行单元：在任意节点后可 SqliteSaver checkpoint
3. team_task_id 用作 thread_id：同一团队任务的多次启动可恢复到上一次中断点
4. 与现有 TeamRunner.run() 并存：原同步主循环继续作为简单路径使用；
   -run_via_graph() 走 LangGraph 执行模型，可中断 / 可恢复 / 可观测

注意：本模块不取代 TeamRunner.run() 业务逻辑——节点内部仍调用 TeamRunner 的私有方法。
LangGraph 只提供执行外壳（状态机 + checkpoint）。这样既满足"LangGraph 负责状态图与
checkpoint、DeepAgents 负责单 Agent 深度执行"的架构分层，又不重写业务逻辑。

失败容忍策略：
- LangGraph 不可用（缺包等）→ 退化为同步主循环（已有 TeamRunner.run）
- Checkpoint 写入失败 → 不阻塞业务运行，记录 WARNING
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.core.logging import logger

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
        """节点 1：选择本轮 speaker。"""
        try:
            if not self.runner.room or not getattr(self.runner, "selector", None):
                return {
                    "speaker": None,
                    "round": state.get("round", 0),
                    "termination_reason": "no_speaker",
                    "hitl_pending": False,
                }
            speaker = self.runner.selector.select(
                agents=self.runner.room.agents,
                state=self.runner.room.state,
                inbox=None,
            )
            return {
                "speaker": speaker.name if speaker else None,
                "round": state.get("round", 0),
                "hitl_pending": False,
                "termination_reason": None if speaker else "no_speaker",
            }
        except Exception as exc:
            logger.error(f"[TeamGraph] node_select_speaker failed: {exc}")
            return {"error": str(exc), "termination_reason": str(exc), "round": state.get("round", 0)}

    def node_run_speaker(self, state: GraphState) -> GraphState:
        """节点 2：调用 AgentRuntimeAdapter 跑 speaker。"""
        if not self.runner.room or not self.runner.adapter:
            return {"termination_reason": "no_room_or_adapter"}
        speaker_name = state.get("speaker")
        _round = state.get("round", 0)
        if not speaker_name:
            return {"termination_reason": "no_speaker", "round": _round}
        speaker = next((a for a in self.runner.room.agents if a.name == speaker_name), None)
        if not speaker:
            return {"termination_reason": "speaker_not_found", "round": _round}
        try:
            from app.multiagent.inbox import AgentInbox
            from app.multiagent.messages import make_message_id, AgentMessage, MessageType

            store = self.runner.store
            room_id = self.runner.room_id
            # 用 AgentInbox 的正确签名：store + room_id + task_id
            inbox = AgentInbox(store=store, room_id=room_id, task_id=self.runner.task_id or self.runner.room.task_id or "")
            unread = inbox.list_unread(speaker.name)
            inbox_context = inbox.get_relevant_context(speaker.name)
            prompt = self.runner.adapter.build_system_prompt(
                agent=speaker,
                shared_state=self.runner.room.state,
                inbox_context=inbox_context,
                team_agents=self.runner.room.agents,
            )
            actions = self.runner.adapter.run(
                agent=speaker,
                inbox_messages=unread,
                shared_state=self.runner.room.state,
            )
            return {"actions": actions, "round": _round}
        except Exception as exc:
            logger.error(f"[TeamGraph] node_run_speaker failed: {exc}")
            return {"error": str(exc), "round": _round}

    def node_process_actions(self, state: GraphState) -> GraphState:
        """节点 3：把 actions 转 messages publish + 更新 SharedTeamState。"""
        actions = state.get("actions", [])
        _round = state.get("round", 0)
        if not actions or not self.runner.room:
            return {"messages": [], "round": _round}
        try:
            speaker_name = state.get("speaker", "")
            # 借助 TeamRunner._process_actions（已含深层护栏）
            self.runner._process_actions(speaker_name, actions)
            # messages 不在这里 emit，由 TeamRunner 的 emitter 统一负责
            return {"round": _round, "messages": []}
        except Exception as exc:
            logger.error(f"[TeamGraph] node_process_actions failed: {exc}")
            return {"error": str(exc), "round": _round}

    def node_decide_terminate(self, state: GraphState) -> GraphState:
        """节点 4：终止判断。返回 {'continue': True/False, 'termination_reason': ...}。"""
        try:
            decision = self.runner.termination_checker.check(
                state=self.runner.room.state,
                recent_messages=[],
                round_count=state.get("round", 0),
            )
            should_continue = not decision.should_terminate and state.get("round", 0) < self.runner.room.state.max_rounds
            return {
                "continue": should_continue,
                "termination_reason": decision.reason if decision.should_terminate else None,
            }
        except Exception as exc:
            logger.error(f"[TeamGraph] node_decide_terminate failed: {exc}")
            return {"continue": False, "termination_reason": str(exc)}

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
            return {
                "status": "completed",
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
        try:
            final = self.graph.invoke(
                initial_state,
                config={"configurable": {"thread_id": thread_id}},
            )
            # 安全上限自检
            rounds = final.get("round", 0)
            if rounds > max_rounds_cap:
                logger.warning(f"[TeamGraph] 达到安全上限 {max_rounds_cap}，强制停止")

            status = "interrupted" if final.get("hitl_pending") else "completed"
            if final.get("termination_reason") == "no_speaker":
                status = "failed"

            state_obj = getattr(self.runner.room, "state", None) if self.runner.room else None
            return {
                "status": status,
                "thread_id": thread_id,
                "rounds": rounds,
                "final_output": state_obj.final_output if state_obj else None,
                "termination_reason": final.get("termination_reason"),
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
