# 多智能体运行时架构

## 一、当前架构（单 Agent）

```
FastAPI / CLI
  ↓
TaskService → TaskStore (SQLite: tasks / task_events / task_messages / artifacts)
  ↓
TaskRunner
  ↓
build_agent() → create_deep_agent(DeepAgentState + tools + backend + checkpointer + ...)
  ↓
agent.invoke({"messages": [(user, input)]})
  ↓
TaskResult → TaskStore.mark_completed()
```

关键特征：
- 一个任务对应一个 DeepAgent 实例
- 所有消息在同一条 messages 链中
- 使用 LangGraph SqliteSaver 做检查点
- 支持 HITL（human-in-the-loop）中断/恢复
- 使用 DeepAgents 原生 subagents（researcher/coder/reviewer 异步子智能体）

## 二、目标架构

```
FastAPI / CLI
  ↓
TaskService（保留原有单 Agent 路径）
  ↓
MultiAgentTeamRunner
  ↓
TeamRoom / Environment
  ├── MessageBus          → AgentInbox (per agent)
  ├── SharedTeamState      → phase / issues / decisions / artifacts
  ├── SpeakerSelector      → rule-first → LLM fallback
  ├── TerminationChecker   → review_passed / max_rounds / stale
  └── ReviewRepairLoop     → review_request → critique → revision → repeat
       ↓
AgentRuntimeAdapter
  ├── PlannerAgent    → create_deep_agent()
  ├── ResearcherAgent → create_deep_agent()
  ├── CoderAgent      → create_deep_agent()
  ├── ReviewerAgent   → create_deep_agent()
  ├── TesterAgent     → create_deep_agent()
  └── FinalizerAgent  → create_deep_agent()
```

### 多 Agent 核心循环

```
1. 用户提交 TeamTask → TeamRoom 创建
2. 用户 Goal 作为 system message 发布到 MessageBus
3. loop:
   a. SpeakerSelector 选择下一发言 Agent
   b. 从 MessageBus 读取该 Agent 的 inbox
   c. 构造 Agent 的 system_prompt（含角色边界 + shared_state + inbox）
   d. AgentRuntimeAdapter 调用 create_deep_agent().invoke()
   e. 解析输出为 actions（send_message / update_state / create_artifact / ...)
   f. publish actions 到 MessageBus
   g. update SharedTeamState
   h. emit task events → 前端 SSE
   i. check termination
4. finalize → 输出 final_result
```

## 三、新增模块

| 模块 | 文件 | 职责 |
|---|---|---|
| Models | `app/multiagent/models.py` | AgentMessage, MessageType, TeamSpec, TeamRunConfig |
| Messages | `app/multiagent/messages.py` | AgentMessage builder, MessageType enum |
| State | `app/multiagent/state.py` | SharedTeamState, TeamDecision, TeamIssue |
| Agent Spec | `app/multiagent/agent_spec.py` | AgentSpec, AgentSubscription |
| Bus | `app/multiagent/bus.py` | MessageBus: publish/broadcast/direct, routing |
| Inbox | `app/multiagent/inbox.py` | AgentInbox: per-agent unread/summarize |
| Room | `app/multiagent/room.py` | TeamRoom lifecycle |
| Store | `app/multiagent/store.py` | SQLite persistence for multiagent entities |
| Runtime Adapter | `app/multiagent/runtime_adapter.py` | Wrap DeepAgents per agent |
| Speaker Selector | `app/multiagent/speaker_selector.py` | Rule-first → LLM fallback |
| Termination | `app/multiagent/termination.py` | Termination conditions |
| Review Repair | `app/multiagent/review_repair.py` | Review-Repair Loop |
| Team Runner | `app/multiagent/team_runner.py` | Core loop orchestration |
| Default Teams | `app/multiagent/default_teams.py` | software_dev_team, research_team |
| Prompts | `app/multiagent/prompts.py` | System prompts for each agent role |
| Policies | `app/multiagent/policies.py` | Team run policies |
| API | `app/api/routes_team.py` | REST endpoints for team tasks |
| CLI | `app/cli.py` (扩展) | `team` subcommand |

## 四、数据模型

### AgentMessage

```python
class AgentMessage:
    id: str
    task_id: str
    room_id: str
    from_agent: str
    to_agent: str | list[str] | None  # None = broadcast
    visibility: "broadcast" | "direct" | "system"
    message_type: MessageType
    content: str
    cause_by: str | None
    reply_to: str | None
    thread_id: str | None
    requires_response: bool
    expected_response_type: str | None
    evidence: list[dict]
    artifact_refs: list[dict]
    metadata: dict
    created_at: datetime
```

### MessageType

user_request, plan, delegation, question, answer, observation, tool_result, proposal, critique, revision_plan, revision_done, review_request, review_result, handoff, decision, state_update, artifact_created, final, error

### SharedTeamState

```python
class SharedTeamState:
    goal: str
    phase: TeamPhase  # created → planning → discussing → executing → reviewing → repairing → finalizing → completed/failed/cancelled
    plan: str
    current_round: int
    open_questions: list[str]
    issues: list[TeamIssue]
    decisions: list[TeamDecision]
    artifacts: list[TeamArtifactRef]
    completed_steps: list[str]
    blocked_steps: list[str]
    review_status: str | None
    final_output: str | None
    metadata: dict
```

### AgentSpec

```python
class AgentSpec:
    name: str
    role: str
    goal: str
    system_prompt: str | None
    watched_message_types: list[MessageType]
    allowed_tools: list[str]
    permissions: list[str]
    runtime_type: str = "deepagents"
    private_memory_scope: str | None
```

### TeamSpec

```python
class TeamSpec:
    name: str
    description: str
    agents: list[AgentSpec]
    max_rounds: int = 20
    termination_policy: str = "review_passed_or_max_rounds"
    review_required: bool = True
    max_review_cycles: int = 3
```

## 五、API 设计

### 新增端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/team-tasks` | 创建多 Agent 任务 |
| GET | `/api/team-tasks/{task_id}` | 任务详情 |
| GET | `/api/team-tasks/{task_id}/events` | 事件流 |
| GET | `/api/team-tasks/{task_id}/messages` | Agent 消息流 |
| GET | `/api/team-tasks/{task_id}/state` | SharedState |
| GET | `/api/team-tasks/{task_id}/agents` | Agent 列表 |
| POST | `/api/team-tasks/{task_id}/messages` | 人工注入消息 |
| POST | `/api/team-tasks/{task_id}/cancel` | 取消任务 |

### 请求示例

```json
POST /api/team-tasks
{
  "goal": "分析当前项目并生成多智能体改造方案",
  "team": "software_dev_team",
  "max_rounds": 20,
  "review_required": true
}
```

## 六、兼容策略

1. **现有单 Agent 路径不变**：`/chat`、`/tasks/*` 继续使用原有 TaskRunner
2. **新增 TeamTask 路径**：使用新的 `app/multiagent/` 模块，不修改现有 `app/task/runner.py`
3. **共享 SQLite 数据库**：在新表上扩展，不破坏旧表
4. **TeamTask 映射到原有 task_id**：每个 TeamTask 在 `tasks` 表中也有一条记录，用于统一展示
5. **AgentMessage 可转换为 TaskMessage**：兼容前端消息展示

## 七、与 MetaGPT 思想对应

| MetaGPT | 本项目 |
|---|---|
| Environment | TeamRoom |
| Role | AgentSpec / TeamAgent |
| Action | Agent action protocol (send_message / update_state / ...) |
| Message | AgentMessage |
| Role.watch(cause_by) | AgentSubscription / watched_message_types |
| Role.msg_buffer | AgentInbox |
| SharedState | SharedTeamState |
| n/a (MetaGPT 无 Speaker Selector) | SpeakerSelector |
| n/a (MetaGPT 固定轮次) | TerminationChecker |
| n/a (MetaGPT 无 Review-Repair Loop) | ReviewRepair Loop |

## 八、当前架构图

```
app/
├── multiagent/              # 新增：多智能体运行时
│   ├── __init__.py
│   ├── models.py            # 核心模型
│   ├── messages.py          # AgentMessage, MessageType
│   ├── state.py             # SharedTeamState
│   ├── agent_spec.py        # AgentSpec, TeamSpec
│   ├── bus.py               # MessageBus
│   ├── inbox.py             # AgentInbox
│   ├── room.py              # TeamRoom
│   ├── store.py             # SQLite 持久化
│   ├── runtime_adapter.py   # DeepAgents 复用层
│   ├── speaker_selector.py  # 发言者选择
│   ├── termination.py       # 终止检查
│   ├── review_repair.py     # 评审返工循环
│   ├── team_runner.py       # 核心循环
│   ├── prompts.py           # 角色提示词
│   ├── policies.py          # 运行策略
│   └── default_teams.py     # 默认团队配置
├── api/
│   └── routes_team.py       # 新增：团队任务 API
├── task/                    # 保留：原有单 Agent 路径
└── core/                    # 保留：原有核心
```

## 九、测试策略

- 单元测试：MessageBus 消息路由、AgentInbox 隔离、SharedState CRUD
- 规则测试：SpeakerSelector 规则优先级、TerminationChecker 条件
- 集成测试：Review-Repair Loop 完整流程
- 兼容测试：原有单 Agent 任务路径不受影响
