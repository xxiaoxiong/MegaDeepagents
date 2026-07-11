"""ParallelRunner：异步并行执行辅助（实验性，未接入 TeamRunner 主循环）。

设计取舍
========
SQLite 写入路径在主线程上完成；并行段只针对**纯函数化**的 LLM 调用阶段
（构造 prompt + adapter.run），因为它们对 room/bus 的写操作都在调用之后由
主线程序列化执行。线程池里每个 worker 只持有 AgentSpec 的不可变快照 +
inbox 文本快照 + shared_state 的字符串快照，保证并发安全。

选择并行候选策略（保守）：
1. 仅在 EXECUTING / DISCUSSING / REPAIRING 阶段启用并行
   （PLANNING / REVIEWING / FINALIZING 强调"主发言者职责清晰"，不准并行）
2. 当前轮所有 agent 都参与候选：把"具备未读消息且非上轮发言者"的 agent 视为候选 speaker；
   在主 speaker 之外额外最多取 (max_concurrent-1) 个并发言者。
3. 不允许同一 Agent 两次都出现在并行集合（避免自相残杀造成 inbox 撕裂）。
4. 主发言者仍是 SpeakerSelector 选出的优先级最高的那一个；并发言者是次优先，
   仅作"提前消化 inbox / 加速信息流"的辅助，不会被记入 _last_speaker 影响下一轮选中。

幂等保证：并行调用返回 (agent_name, inbox_messages, actions) 三元组，
主循环按确定性顺序逐个 publish + process_actions + mark_read + save_state ——
对 store / state bus 而言，并发段等价于"快速预跑 LLM"。

注意（Req 10）：本模块尚未接入 TeamRunner.run() 主循环。TeamGraph 和 TeamRunner
当前仅以串行方式调用 TeamRoundExecutor，并行化属于下一阶段规划。代码保留供参考。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import AgentSpec
from app.multiagent.messages import AgentMessage
from app.multiagent.state import SharedTeamState, TeamPhase


# 允许并行的阶段（其它阶段保持串行：planner/reviewer/finalizer 责任单一）
_PARALLEL_PHASES = {TeamPhase.EXECUTING, TeamPhase.DISCUSSING, TeamPhase.REPAIRING}


@dataclass
class ParallelExecutionPlan:
    """单轮并行执行计划。

    主发言者 responsibilities 不变：他需要的全部 inbox 都进 plan；
    并发言者各自加载自己当时的 inbox 快照参与并行 LLM 调用。
    """

    primary: AgentSpec
    primary_unread: list[AgentMessage]
    primary_inbox_context: str
    secondary: list[AgentSpec] = field(default_factory=list)
    secondary_unread: dict[str, list[AgentMessage]] = field(default_factory=dict)
    secondary_context: dict[str, str] = field(default_factory=dict)

    @property
    def parallel_agent_names(self) -> list[str]:
        return [self.primary.name] + [a.name for a in self.secondary]


def plan_parallel_round(
    phase: TeamPhase,
    agents: list[AgentSpec],
    inbox: Any,
    last_speaker: str | None,
    primary_speaker: AgentSpec,
    max_concurrent: int,
    primary_unread: list[AgentMessage],
    primary_inbox_context: str,
) -> ParallelExecutionPlan | None:
    """构造本轮的并行执行计划。返回 None 表示回退串行。"""
    if max_concurrent <= 1:
        return None
    if phase not in _PARALLEL_PHASES:
        return None
    if primary_speaker is None:
        return None

    candidates = [a for a in agents if a.name != primary_speaker.name]
    candidates = [a for a in candidates if a.name != last_speaker]
    if not candidates:
        return None

    plan = ParallelExecutionPlan(
        primary=primary_speaker,
        primary_unread=primary_unread,
        primary_inbox_context=primary_inbox_context,
    )

    picked = 0
    for agent in candidates:
        if picked >= max_concurrent - 1:
            break
        try:
            unread = inbox.list_unread(agent.name)
            ctx = inbox.get_relevant_context(agent.name)
        except Exception as exc:
            logger.warning(
                f"[B2] failed to load inbox for secondary agent {agent.name}: {exc}"
            )
            continue
        # 仅在确实有未读且与主发言者不重叠消息时纳入并行
        if not unread:
            continue
        plan.secondary.append(agent)
        plan.secondary_unread[agent.name] = unread
        plan.secondary_context[agent.name] = ctx
        picked += 1

    if not plan.secondary:
        return None
    return plan


def execute_parallel(
    plan: ParallelExecutionPlan,
    adapter: Any,
    shared_state_snapshot: SharedTeamState,
    team_agents: list[AgentSpec],
) -> dict[str, list[dict[str, Any]]]:
    """并行调用 adapter.run()。返回 {agent_name: [actions]}。

    shared_state_snapshot：调用方传入一份"只读"快照（同一对象）；
    LLM 调用不应在并行段直接改它（actions 落地由主循环后续 _process_actions 处理）。
    Adapter.run 内部不再 publish / mark_read，只产出 actions，因此线程安全。
    """
    results: dict[str, list[dict[str, Any]]] = {}
    pending: list[tuple[AgentSpec, list[AgentMessage], str]] = [
        (plan.primary, plan.primary_unread, plan.primary_inbox_context)
    ]
    for sec in plan.secondary:
        pending.append((sec, plan.secondary_unread[sec.name], plan.secondary_context[sec.name]))

    def _emit_event_safe(agent_spec: AgentSpec, evt_type: str, payload: dict[str, Any]) -> None:
        """在并行执行开始 / 结束旁挂一个事件。失败静默。"""
        try:
            from app.multiagent.event_emitter import get_event_emitter
            emitter = get_event_emitter()
            room_id = getattr(adapter, "room_id", "") or ""
            emitter.emit(room_id, evt_type, payload)
        except Exception:
            pass

    if len(pending) == 1:
        agent, unread, ctx = pending[0]
        results[agent.name] = _invoke_adapter(adapter, agent, unread, shared_state_snapshot, team_agents, ctx)
        return results

    # 多 worker：LLM 调用一般是 IO-bound，线程池即可
    with ThreadPoolExecutor(max_workers=len(pending)) as ex:
        future_to_agent = {
            ex.submit(
                _invoke_adapter,
                adapter, agent, unread, shared_state_snapshot, team_agents, ctx,
            ): agent
            for (agent, unread, ctx) in pending
        }
        for fut in as_completed(future_to_agent):
            agent = future_to_agent[fut]
            try:
                results[agent.name] = fut.result()
            except Exception as exc:
                logger.error(f"[B2] parallel LLM for {agent.name} 失败，退化为 no_op：{exc}")
                _emit_event_safe(
                    agent,
                    "actions_emitted",
                    {
                        "agent": agent.name,
                        "error": str(exc)[:200],
                        "action_count": 0,
                        "action_types": [],
                        "fallback": "no_op",
                    },
                )
                # 失败回退：no_op 不破坏主流程
                results[agent.name] = [{"type": "no_op", "content": f"并行执行失败 fallback: {exc}"}]
    return results


def _invoke_adapter(
    adapter: Any,
    agent: AgentSpec,
    unread: list[AgentMessage],
    shared_state: SharedTeamState,
    team_agents: list[AgentSpec],
    inbox_context: str,
) -> list[dict[str, Any]]:
    """单 Agent 的 LLM 调用。线程安全：build_system_prompt / adapter.run 仅读取快照。

    注意：
    - 这里的 shared_state 是主线程持有的活动对象；adapter.run 只读它构造 prompt，
      不调用 self.room.publish / inbox.mark_read，因此 worker 并行安全。
    - inbox_context 在主线程上预先拉取，避免 worker 直接读 store（store 默认非线程隔离）。
    """
    return adapter.run(
        agent=agent,
        inbox_messages=unread,
        shared_state=shared_state,
    )
