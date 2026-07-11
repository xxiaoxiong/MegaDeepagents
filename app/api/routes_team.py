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


# ========== Endpoints ==========


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

    out: list[TeamTaskMetaResponse] = []
    for r in rows:
        room_id = r["room_id"] if isinstance(r, dict) else r[1]
        task_id = r["task_id"] if isinstance(r, dict) else r[0]
        if not room_id:
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
    """创建并启动多 Agent 团队任务。"""
    available = _list_teams()
    if req.team not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Team '{req.team}' not found. Available: {available}",
        )
    runner = TeamRunner.create(
        goal=req.goal,
        team_name=req.team,
        max_rounds=req.max_rounds,
        review_required=req.review_required,
    )
    # 后台运行：用 copy_context 传播 contextvar（含 LangSmith 的 _PARENT_RUN_TREE_REF），
    # 否则跨线程会丢失 trace 父子链，LangSmith 上所有 run 变成独立根。
    import threading
    import contextvars

    from app.multiagent.team_runner import _run_team_traced

    def _safe_run():
        try:
            _run_team_traced(runner)
        except Exception as exc:
            logger.error(f"[TeamTask] run failed for task={runner.task_id}: {exc}")

    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(_safe_run,), daemon=True)
    thread.start()
    # 在响应中暴露 EfficientRunPolicy 派生字段（Req 6：API 必须暴露真实生效的策略）
    policy = runner.effective_policy
    return CreateTeamTaskResponse(
        task_id=runner.task_id,
        room_id=runner.room_id,
        status="running",
        max_rounds=policy.max_rounds,
        review_required=policy.review_required,
        max_review_cycles=policy.max_review_cycles,
    )


@router.get("/team-tasks/{task_id}", response_model=TeamTaskMetaResponse)
def get_team_task(task_id: str):
    """查询多 Agent 任务状态。"""
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
def get_team_task_messages(task_id: str):
    """获取多 Agent 任务的消息流。可在 AgentMessage 之间看到 to/from 关系。"""
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
def get_team_task_state(task_id: str):
    """获取当前共享团队状态。"""
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
def get_team_task_agents(task_id: str):
    """获取多 Agent 任务中的所有 Agent 列表。"""
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
def inject_team_task_message(task_id: str, msg: CreateTeamTaskRequest):
    """人工向多 Agent 任务注入新消息。"""
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
def cancel_team_task(task_id: str):
    """取消多 Agent 任务。"""
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
