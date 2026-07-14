"""TeamTask API routes: 多 Agent 任务 REST 端点。

约定：所有端点使用 /api/team-tasks 前缀。
保留原有 /tasks/* 单 Agent 路径。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.logging import logger
from app.multiagent.default_teams import list_teams as _list_teams
from app.multiagent.event_emitter import get_event_emitter
from app.multiagent.messages import AgentMessage, MessageType, make_message_id
from app.multiagent.team_runner import TeamRunner
from app.multiagent.team_runtime import get_team_runtime
from app.multiagent.team_run_context import TeamRunMode
from app.multiagent.store import get_multiagent_store

router = APIRouter()


# ========== Request / Response models ==========


class CreateTeamTaskRequest(BaseModel):
    goal: str = Field(..., description="任务目标")
    team: str = Field(default="software_dev_team", description="团队模板名")
    max_rounds: int = Field(default=20, ge=1, le=200)
    review_required: bool = Field(default=True)


class CreateTeamTaskResponse(BaseModel):
    task_id: str
    room_id: str
    status: str
    max_rounds: int = 20
    review_required: bool = True
    max_review_cycles: int = 3


class TeamRunMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)


class TeamTaskMetaResponse(BaseModel):
    task_id: str
    room_id: str
    goal: str
    team_name: str
    status: str
    phase: str | None = None
    current_round: int | None = None
    max_rounds: int | None = None
    review_required: bool | None = None
    max_review_cycles: int | None = None
    agents: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class TeamStateResponse(BaseModel):
    goal: str
    phase: str
    plan: str
    current_round: int
    max_rounds: int
    open_questions: list[str] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    blocked_steps: list[str] = Field(default_factory=list)
    review_status: str | None = None
    review_cycles: int = 0
    final_output: str | None = None


class MessageResponse(BaseModel):
    id: str
    from_agent: str
    to_agent: str | list[str] | None = None
    visibility: str
    message_type: str
    content: str
    cause_by: str | None = None
    reply_to: str | None = None
    requires_response: bool = False
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str


class RoundResponse(BaseModel):
    round_number: int
    selected_speaker: str
    action_summary: str = ""
    message_ids: list[str] = Field(default_factory=list)
    termination_reason: str | None = None
    langsmith_run_url: str | None = None
    created_at: str


async def _task_team_run(run_id: str) -> dict[str, Any] | None:
    """Return a TASK_TEAM run from the unified control plane, if it exists."""
    run = await get_team_runtime().get_run(run_id)
    if not run:
        return None
    mode = run.get("mode")
    mode_value = getattr(mode, "value", mode)
    return run if mode_value == TeamRunMode.TASK_TEAM.value else None


def _task_team_agents(run_id: str) -> list[dict[str, Any]]:
    """Read teammates from the runtime registry, then durable history on cold runs."""
    from app.multiagent.agent_registry import get_agent_registry
    agents = get_agent_registry().list_by_run(run_id)
    if agents:
        return [agent.model_dump(mode="json") for agent in agents]
    from app.multiagent.phase_g_store import get_agent_run_history
    return get_agent_run_history().list_by_run(run_id)


def _task_team_meta(run_id: str, run: dict[str, Any]) -> TeamTaskMetaResponse:
    agents = _task_team_agents(run_id)
    ctx = run.get("ctx")
    created_at = ctx.created_at if ctx is not None else run.get("created_at", "")
    created_at = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    updated_at = run.get("updated_at", "")
    updated_at = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
    return TeamTaskMetaResponse(
        task_id=run_id, room_id=run_id,
        goal=run.get("goal", ""),
        team_name=run.get("team_name", run.get("team_id", "")),
        status=run.get("status", "unknown"),
        phase=run.get("status"), current_round=None,
        max_rounds=run.get("max_rounds"),
        review_required=run.get("review_required"),
        agents=[agent.get("name", agent.get("agent_id", "")) for agent in agents],
        created_at=created_at, updated_at=updated_at,
    )


# ========== Endpoints ==========


# New control-plane API.  The old /team-tasks endpoints remain compatibility
# routes, but new clients must not touch TeamRunner / TeamRoom.
@router.post("/team-runs")
async def create_team_run(req: CreateTeamTaskRequest):
    runtime = get_team_runtime()
    ctx = await runtime.create_run(
        goal=req.goal, team_name=req.team, mode=TeamRunMode.TASK_TEAM,
        max_rounds=req.max_rounds, review_required=req.review_required,
    )
    import asyncio
    asyncio.create_task(runtime.start_run(ctx, req.goal, req.team, req.max_rounds, req.review_required))
    return {"run_id": ctx.run_id, "status": "running"}


@router.get("/team-runs/{run_id}")
async def get_team_run(run_id: str):
    run = await get_team_runtime().get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Team run not found")
    return {"run_id": run_id, "status": run.get("status"), "goal": run.get("goal"),
            "team_name": run.get("team_name", run.get("team_id"))}


@router.post("/team-runs/{run_id}/cancel")
async def cancel_team_run(run_id: str):
    if not await get_team_runtime().cancel_run(run_id):
        raise HTTPException(status_code=404, detail="Team run not found")
    return {"run_id": run_id, "status": "cancelled"}


@router.post("/team-runs/{run_id}/resume")
async def resume_team_run(run_id: str):
    if not await get_team_runtime().resume_run(run_id):
        raise HTTPException(status_code=404, detail="Team run not resumable")
    return {"run_id": run_id, "status": "running"}


@router.get("/team-runs/{run_id}/agents")
def get_team_run_agents(run_id: str):
    from app.multiagent.agent_registry import get_agent_registry
    agents = get_agent_registry().list_by_run(run_id)
    if not agents:
        from app.multiagent.phase_g_store import get_agent_run_history
        return get_agent_run_history().list_by_run(run_id)
    return [agent.model_dump(mode="json") for agent in agents]


@router.get("/team-runs/{run_id}/tasks")
def get_team_run_tasks(run_id: str):
    from app.multiagent.task_board import get_task_board
    return [task.model_dump(mode="json") for task in get_task_board().list_by_run(run_id)]


@router.get("/team-runs/{run_id}/artifacts")
def get_team_run_artifacts(run_id: str):
    from app.multiagent.phase_g_store import get_agent_run_history
    return get_agent_run_history().list_artifacts_by_run(run_id)


@router.get("/team-runs/{run_id}/events")
def get_team_run_events(run_id: str):
    from app.multiagent.phase_g_store import get_agent_run_history
    return get_agent_run_history().list_events(run_id)


@router.get("/team-runs/{run_id}/messages")
def get_team_run_messages(run_id: str):
    from app.multiagent.mailbox import get_mailbox
    return [message.model_dump(mode="json") for message in get_mailbox().list_messages_in_run(run_id)]


@router.post("/team-runs/{run_id}/agents/{agent_id}/messages")
async def send_team_run_message(run_id: str, agent_id: str, req: TeamRunMessageRequest):
    if not await get_team_runtime().send_message(run_id, agent_id, req.content):
        raise HTTPException(status_code=404, detail="Agent or run not found")
    return {"run_id": run_id, "agent_id": agent_id, "status": "delivered"}


@router.post("/team-runs/{run_id}/agents/{agent_id}/pause")
async def pause_team_run_agent(run_id: str, agent_id: str):
    if not await get_team_runtime().pause_agent(run_id, agent_id):
        raise HTTPException(status_code=409, detail="Agent is not an idle teammate in this run")
    return {"run_id": run_id, "agent_id": agent_id, "status": "blocked"}


@router.post("/team-runs/{run_id}/agents/{agent_id}/resume")
async def resume_team_run_agent(run_id: str, agent_id: str):
    if not await get_team_runtime().resume_agent(run_id, agent_id):
        raise HTTPException(status_code=409, detail="Agent is not paused in this run")
    return {"run_id": run_id, "agent_id": agent_id, "status": "idle"}


@router.post("/team-runs/{run_id}/agents/{agent_id}/stop")
async def stop_team_run_agent(run_id: str, agent_id: str):
    if not await get_team_runtime().stop_agent(run_id, agent_id):
        raise HTTPException(status_code=409, detail="Agent is not active in this run")
    return {"run_id": run_id, "agent_id": agent_id, "status": "stopping"}


@router.get("/teams")
def list_available_teams():
    """列出所有可用团队模板（B4 前端用）。"""
    return [
        {"name": name, "description": "", "agents": []}
        for name in _list_teams()
    ]


@router.get("/team-tasks", response_model=list[TeamTaskMetaResponse])
def list_team_tasks(limit: int = 50):
    """列出所有多 Agent 任务（B4 前端用，最近创建在前）。

    实现上从 store 的 team_rooms 表读所有 room，逆序返回。
    """
    runtime = get_team_runtime()
    task_team_rows = [
        row for row in runtime.list_run_records(limit)
        if getattr(row.get("mode"), "value", row.get("mode")) == TeamRunMode.TASK_TEAM.value
    ]
    out: list[TeamTaskMetaResponse] = [
        _task_team_meta(row["run_id"], row)
        for row in task_team_rows
    ]
    known_task_ids = {entry.task_id for entry in out}

    store = get_multiagent_store()
    # store 暴露的接口未直接给 list_rooms，这里用前 N 个 task_id 反查
    # 兼容：若 store 有 list_rooms 方法优先用
    if hasattr(store, "list_rooms"):
        rows = store.list_rooms(limit=limit)
    else:
        # fallback：直接查 SQLite
        rows = store.conn.execute(
            "SELECT task_id, room_id FROM team_rooms ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        rows = [{"task_id": r[0], "room_id": r[1]} for r in rows]

    for r in rows:
        room_id = r["room_id"] if isinstance(r, dict) else r[1]
        task_id = r["task_id"] if isinstance(r, dict) else r[0]
        if not room_id:
            continue
        if task_id in known_task_ids:
            continue
        meta = store.load_room(room_id)
        if not meta:
            continue
        state = store.load_state(room_id)
        agents = store.load_agents(room_id)
        # 从有效策略推断 review_required（复用 store 中可恢复的信息）
        review_required = None
        max_review_cycles = None
        if meta.get("config"):
            config = meta["config"]
            team_spec = meta.get("team_spec")
            if team_spec:
                from app.multiagent.policies import EffectiveRunPolicy
                policy = EffectiveRunPolicy.from_team_and_run_config(team_spec, config)
                review_required = policy.review_required
                max_review_cycles = policy.max_review_cycles
        out.append(TeamTaskMetaResponse(
            task_id=task_id,
            room_id=room_id,
            goal=meta["config"].goal if meta.get("config") else "",
            team_name=meta["team_spec"].name if meta.get("team_spec") else "",
            status=meta.get("status", "unknown"),
            phase=state.phase.value if state else None,
            current_round=state.current_round if state else None,
            max_rounds=state.max_rounds if state else None,
            review_required=review_required,
            max_review_cycles=max_review_cycles,
            agents=[a.name for a in agents],
            created_at="",
            updated_at="",
        ))
    return out


@router.post("/team-tasks", response_model=CreateTeamTaskResponse)
def create_team_task(req: CreateTeamTaskRequest):
    """创建并启动多 Agent 团队任务。

    默认走 TASK_TEAM 模式（TeamRuntimeFacade → ParallelTeamScheduler）。
    保留旧 TeamRunner（DISCUSSION 模式）作为可选项。

    设计：
    - create_run 轻量同步完成（只建 TeamRunContext，不涉及 LLM），
      让 API 能立即返回 task_id。
    - start_run 在后台线程执行（涉及 LLM，可以是长耗时操作）。
    - 使用 asyncio.new_event_loop() / set_event_loop() 避免嵌套 loop 冲突。
    """
    available = _list_teams()
    if req.team not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Team '{req.team}' not found. Available: {available}",
        )

    import asyncio
    import threading
    import contextvars
    from app.multiagent.team_runtime import get_team_runtime

    runtime = get_team_runtime()

    # 1. 同步创建 run（轻量，不涉及 LLM）
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ctx = loop.run_until_complete(runtime.create_run(
            goal=req.goal,
            team_name=req.team,
            mode=TeamRunMode.TASK_TEAM,
            max_rounds=req.max_rounds,
            review_required=req.review_required,
        ))
        loop.close()
    except Exception as exc:
        logger.error(f"[TeamTask] create_run failed: {exc}")
        raise HTTPException(status_code=500, detail=f"create_run failed: {exc}")

    # 2. 后台线程执行 start_run（涉及 LLM 调用）
    def _bg_run():
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            result = new_loop.run_until_complete(runtime.start_run(
                ctx=ctx, goal=req.goal, team_name=req.team,
                max_rounds=req.max_rounds, review_required=req.review_required,
            ))
            new_loop.close()
            logger.info(f"[TeamTask] run completed: id={ctx.run_id} status={result.status}")
        except Exception as exc:
            logger.error(f"[TeamTask] start_run failed for {ctx.run_id}: {exc}")

    thread = threading.Thread(target=_bg_run, daemon=True)
    thread.start()

    return CreateTeamTaskResponse(
        task_id=ctx.run_id,
        room_id=ctx.run_id,
        status="running",
        max_rounds=req.max_rounds,
        review_required=req.review_required,
    )


@router.get("/team-tasks/{task_id}", response_model=TeamTaskMetaResponse)
async def get_team_task(task_id: str):
    """查询多 Agent 任务状态。"""
    task_team_run = await _task_team_run(task_id)
    if task_team_run is not None:
        return _task_team_meta(task_id, task_team_run)
    store = get_multiagent_store()
    # 先按 task_id 查 room
    meta = store.get_room_by_task(task_id)
    if not meta:
        # 也尝试直接作为 room_id 查
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        meta = room_meta
    room_id = meta["room_id"]
    room_meta = store.load_room(room_id)
    if not room_meta:
        raise HTTPException(status_code=404, detail="Team task not found")
    agents = store.load_agents(room_id)
    state = store.load_state(room_id)
    # 从有效策略推断 review_required
    review_required = None
    max_review_cycles = None
    if room_meta.get("config") and room_meta.get("team_spec"):
        from app.multiagent.policies import EffectiveRunPolicy
        policy = EffectiveRunPolicy.from_team_and_run_config(
            room_meta["team_spec"], room_meta["config"],
        )
        review_required = policy.review_required
        max_review_cycles = policy.max_review_cycles
    return TeamTaskMetaResponse(
        task_id=meta["task_id"],
        room_id=room_id,
        goal=meta["config"].goal if meta.get("config") else "",
        team_name=meta["team_spec"].name if meta.get("team_spec") else "",
        status=meta.get("status", "unknown"),
        phase=state.phase.value if state else None,
        current_round=state.current_round if state else None,
        max_rounds=state.max_rounds if state else None,
        review_required=review_required,
        max_review_cycles=max_review_cycles,
        agents=[a.name for a in agents],
        created_at="",
        updated_at="",
    )


@router.get("/team-tasks/{task_id}/messages", response_model=list[MessageResponse])
async def get_team_task_messages(task_id: str):
    """获取多 Agent 任务的消息流。可在 AgentMessage 之间看到 to/from 关系。"""
    if await _task_team_run(task_id) is not None:
        from app.multiagent.mailbox import get_mailbox
        mailbox = get_mailbox()
        mailbox.restore_from_db(task_id)
        return [
            MessageResponse(
                id=message.message_id, from_agent=message.from_agent_id,
                to_agent=message.to_agent_id, visibility="team",
                message_type="mailbox", content=message.content,
                reply_to=message.reply_to, created_at=message.created_at.isoformat(),
            )
            for message in mailbox.list_messages_in_run(task_id)
        ]
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    messages = store.get_room_messages(room_id)
    return [
        MessageResponse(
            id=m.id,
            from_agent=m.from_agent,
            to_agent=m.to_agent,
            visibility=m.visibility.value,
            message_type=m.message_type.value,
            content=m.content[:2000],
            cause_by=m.cause_by,
            reply_to=m.reply_to,
            requires_response=m.requires_response,
            artifact_refs=m.artifact_refs,
            evidence=m.evidence,
            created_at=m.created_at.isoformat(),
        )
        for m in messages
    ]


@router.get("/team-tasks/{task_id}/state", response_model=TeamStateResponse)
async def get_team_task_state(task_id: str):
    """获取当前共享团队状态。"""
    task_team_run = await _task_team_run(task_id)
    if task_team_run is not None:
        from app.multiagent.task_board import get_task_board
        tasks = get_task_board().list_by_run(task_id)
        return TeamStateResponse(
            goal=task_team_run.get("goal", ""), phase=task_team_run.get("status", "unknown"),
            plan="TaskGraph/TaskBoard", current_round=0,
            max_rounds=int(task_team_run.get("max_rounds") or 0),
            completed_steps=[task.task_id for task in tasks if task.status.value == "succeeded"],
            blocked_steps=[task.task_id for task in tasks if task.status.value in ("failed", "repair_required")],
        )
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    state = store.load_state(room_id)
    if not state:
        raise HTTPException(status_code=404, detail="State not found")
    return TeamStateResponse(
        goal=state.goal,
        phase=state.phase.value,
        plan=state.plan,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        open_questions=state.open_questions,
        issues=[i.model_dump() for i in state.issues],
        decisions=[d.model_dump() for d in state.decisions],
        artifacts=[a.model_dump() for a in state.artifacts],
        completed_steps=state.completed_steps,
        blocked_steps=state.blocked_steps,
        review_status=state.review_status,
        review_cycles=state.review_cycles,
        final_output=state.final_output,
    )


@router.get("/team-tasks/{task_id}/agents")
async def get_team_task_agents(task_id: str):
    """获取多 Agent 任务中的所有 Agent 列表。"""
    if await _task_team_run(task_id) is not None:
        return _task_team_agents(task_id)
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    agents = store.load_agents(room_id)
    return [
        {
            "name": a.name,
            "role": a.role,
            "goal": a.goal,
            "watched_message_types": [t.value for t in a.watched_message_types],
            "allowed_tools": a.allowed_tools,
        }
        for a in agents
    ]


@router.post("/team-tasks/{task_id}/messages")
async def inject_team_task_message(task_id: str, msg: CreateTeamTaskRequest):
    """人工向多 Agent 任务注入新消息。"""
    if await _task_team_run(task_id) is not None:
        agents = _task_team_agents(task_id)
        if not agents:
            raise HTTPException(status_code=409, detail="TASK_TEAM has no teammate to receive the message")
        agent_id = agents[0]["agent_id"]
        if not await get_team_runtime().send_message(task_id, agent_id, msg.goal):
            raise HTTPException(status_code=409, detail="Message delivery failed")
        return {"status": "injected", "task_id": task_id, "agent_id": agent_id}
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    from app.multiagent.room import TeamRoom
    room = TeamRoom.load(room_id, store)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    room.send_system_message(content=msg.goal if hasattr(msg, "goal") else msg.goal)
    return {"status": "injected"}


@router.post("/team-tasks/{task_id}/cancel")
async def cancel_team_task(task_id: str):
    """取消多 Agent 任务。"""
    if await _task_team_run(task_id) is not None:
        if not await get_team_runtime().cancel_run(task_id):
            raise HTTPException(status_code=409, detail="TASK_TEAM cancellation failed")
        return {"status": "cancelled", "task_id": task_id, "room_id": task_id}
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    runner = TeamRunner.load(room_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Failed to load runner")
    ok = runner.cancel()
    if not ok:
        raise HTTPException(status_code=500, detail="Cancel failed")
    return {"status": "cancelled", "task_id": task_id, "room_id": room_id}


@router.get("/team-tasks/{task_id}/rounds", response_model=list[RoundResponse])
def get_team_task_rounds(task_id: str):
    """获取多 Agent 任务的每轮记录。"""
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    rounds = store.list_rounds(room_id)
    return [
        RoundResponse(
            round_number=r["round_number"],
            selected_speaker=r["selected_speaker"],
            action_summary=r.get("action_summary", ""),
            message_ids=(
                json.loads(r.get("message_ids", "[]"))
                if isinstance(r.get("message_ids"), str) else r.get("message_ids", [])
            ),
            termination_reason=r.get("termination_reason"),
            langsmith_run_url=r.get("langsmith_run_url"),
            created_at=r.get("created_at", ""),
        )
        for r in rounds
    ]


# ========== SSE 实时事件端点 ==========


@router.get("/team-tasks/{task_id}/events")
def stream_team_task_events(task_id: str):
    """SSE 实时流式推送多 Agent 任务事件。

    SSE 格式：
    ```
    event: speaker_selected
    data: {"agent": "Planner", "round": 1}

    event: message_published
    data: {"from_agent": "Planner", "message_type": "plan", ...}

    event: termination
    data: {"reason": "review_passed", "round": 5}
    ```

    若房间已 terminated 或不存在，返回 404。
    """
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]

    emitter = get_event_emitter()
    sub = emitter.subscribe(room_id, maxsize=500)

    def _generate() -> Any:
        try:
            for event in sub.sync_iter(timeout=1.0, max_wait=10.0):
                yield f"event: {event['event']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
        finally:
            emitter.unsubscribe(sub)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ========== HITL 端点 ==========


class HITLConflictResponse(BaseModel):
    """HITL 冲突项展示模型。"""
    issue_id: str
    conflict_type: str = "other"
    description: str = ""
    positions: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "open"
    created_at: str = ""


class HITLResolveRequest(BaseModel):
    """人工裁决输入。"""
    decision: str = Field(..., description="裁决决定")
    reason: str = Field(default="", description="裁决理由")


@router.get("/team-tasks/{task_id}/hitl-conflicts", response_model=list[HITLConflictResponse])
def get_hitl_conflicts(task_id: str):
    """获取待处理 HITL 冲突清单。

    查找 room 的 state 中所有 conflict 类型的 blocking issue（owner=None 或
    conflict 标签），返回列表。若没有 HITL 冲突，返回空数组。
    """
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]
    state = store.load_state(room_id)
    if not state:
        return []
    conflicts = [
        i for i in state.issues
        if i.id and (
            "conflict" in i.id.lower()
            or "conflict" in (i.title or "").lower()
            or (i.owner is None and i.severity.value == "high")
        )
    ]
    return [
        HITLConflictResponse(
            issue_id=i.id,
            conflict_type="conflict",
            description=f"{i.title}\n{i.description}",
            positions=i.evidence or [],
            status=i.status.value,
            created_at=i.created_at.isoformat(),
        )
        for i in conflicts
        if i.status.value == "open"
    ]


@router.post("/team-tasks/{task_id}/hitl-resolve/{issue_id}")
def resolve_hitl_conflict(task_id: str, issue_id: str, req: HITLResolveRequest):
    """人工裁决一条 HITL 冲突。

    将裁决写入 Decision，关闭对应的 Issue，使团队循环可继续。
    """
    store = get_multiagent_store()
    meta = store.get_room_by_task(task_id)
    if not meta:
        room_meta = store.load_room(task_id)
        if not room_meta:
            raise HTTPException(status_code=404, detail="Team task not found")
        room_id = task_id
    else:
        room_id = meta["room_id"]

    # 直接从 store 的 team_issues 表更新
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    store.conn.execute(
        "UPDATE team_issues SET status = 'resolved', resolved_at = ? WHERE id = ? AND room_id = ?",
        (now, issue_id, room_id),
    )
    store.conn.commit()

    # 也从 JSON state 更新（save_state 会同步 team_issues 表）
    state = store.load_state(room_id)
    if state:
        from app.multiagent.state import IssueStatus, TeamDecision
        ok = state.resolve_issue(issue_id, IssueStatus.RESOLVED)
        # 添加决策记录
        decision = TeamDecision(
            id=f"hitl_{issue_id}",
            title=f"HITL 裁决：{req.decision[:100]}",
            rationale=req.reason[:300],
            decided_by="human",
        )
        state.add_decision(decision)
        store.save_state(state)  # 含 _sync_decisions / _sync_issues

    return {
        "status": "resolved",
        "issue_id": issue_id,
        "decision": req.decision,
    }
