你现在需要在当前项目 `general-agent-frame` 中做一次架构级改造：将现有“基于 DeepAgents 的单智能体复杂任务运行框架”，升级为“支持多智能体通信协作的 Agent Team Runtime”。

本次改造不是简单增加几个 subagents，也不是做串行/并行执行后汇总结果，而是要实现一个类似 MetaGPT 思路的多智能体通信框架：多个 Agent 能在同一个任务环境中通过结构化消息通信、订阅消息、接收任务、提出问题、交付产物、互相评审、返工修复，最终共同完成复杂任务。

请先完整阅读当前项目代码，再执行改造。不要盲目重写，不要破坏当前已有单 Agent 任务运行能力。现有 DeepAgents、LangChain、LangGraph、LangSmith、workspace、memory、skills、permissions、HITL、task events、task messages、artifacts 等能力都要继续保留，并作为多智能体运行时的底座。

------

# 一、总体目标

将当前项目升级为：

```text
FastAPI / CLI / Web
  ↓
TaskService
  ↓
MultiAgentTeamRunner
  ↓
TeamRoom / Environment
  ├── MessageBus
  ├── AgentInbox
  ├── SharedTeamState
  ├── SpeakerSelector
  ├── TerminationChecker
  ├── ReviewRepairLoop
  └── ArtifactStore
       ↓
AgentRuntimeAdapter
  ├── PlannerAgent    -> DeepAgent Runtime
  ├── ResearcherAgent -> DeepAgent Runtime
  ├── CoderAgent      -> DeepAgent Runtime
  ├── ReviewerAgent   -> DeepAgent Runtime
  └── TesterAgent     -> DeepAgent Runtime
```

最终要做到：

1. 现有单 Agent 任务仍然可以正常运行。
2. 新增多 Agent 任务入口。
3. 一个多 Agent 任务会创建一个 TeamRoom。
4. 多个 Agent 加入 TeamRoom。
5. Agent 之间通过结构化消息通信，而不是简单共用一段聊天记录。
6. 每个 Agent 有自己的 inbox、memory、role、tools、permissions、system prompt。
7. Agent 可以 broadcast，也可以 direct message。
8. Agent 可以订阅某类消息，类似 MetaGPT 的 `Role.watch(cause_by)`。
9. 系统有共享状态 `SharedTeamState`，记录任务目标、阶段、计划、开放问题、决策、产物、阻塞项、评审结果。
10. 系统有 SpeakerSelector，负责决定下一轮哪个 Agent 发言或行动。
11. 系统有 TerminationChecker，避免无限循环。
12. 系统有 ReviewerAgent 和 Review-Repair Loop，结果不合格时能返工。
13. 所有 Agent 消息、事件、状态变更、工具调用、产物都要落库或落盘，便于前端展示、审计和回放。
14. 尽量复用现有 TaskService、TaskRunner、store、events、artifacts、DeepAgents agent_factory，不要重复造已有能力。

------

# 二、参考 MetaGPT 的核心思想，但不要直接照搬

请重点借鉴 MetaGPT 的以下设计思想：

## 1. Environment / TeamRoom

MetaGPT 中多个 Role 在同一个 Environment 中运行，消息通过 Environment 进行分发。

在当前项目中实现：

```text
TeamRoom = 一个多智能体任务环境
```

TeamRoom 负责：

```text
- 管理当前任务的所有 Agent
- 管理 MessageBus
- 管理 SharedTeamState
- 管理 artifacts
- 管理每个 Agent 的 inbox
- 管理任务是否 idle / done / failed
```

## 2. Role / Agent

MetaGPT 中 Role 是智能体角色，Role 有自己的 action、memory、watch、msg_buffer。

在当前项目中实现：

```text
AgentSpec / TeamAgent
```

每个 Agent 至少包含：

```text
- name
- role
- goal
- system_prompt
- allowed_tools
- watched_message_types
- permissions
- runtime_type
- private_memory_scope
```

每个 Agent 通过 `AgentRuntimeAdapter` 调用现有 DeepAgent 能力。

## 3. Action

MetaGPT 中 Action 是 Role 可以执行的动作。

在当前项目中抽象为：

```text
Agent 能力 / Tool / Skill / Runtime Action
```

初期不需要复杂 Action 类体系，但要在 Agent 输出协议中显式区分：

```text
- send_message
- update_state
- create_artifact
- request_review
- respond_critique
- mark_done
- handoff
```

## 4. Message

MetaGPT 的 Message 包含 content、cause_by、sent_from、send_to 等字段。

在当前项目中新增更工程化的 `AgentMessage`：

```python
class AgentMessage:
    id: str
    task_id: str
    room_id: str
    from_agent: str
    to_agent: str | list[str] | None
    visibility: Literal["broadcast", "direct", "system"]
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

MessageType 至少支持：

```text
user_request
plan
delegation
question
answer
observation
tool_result
proposal
critique
revision_plan
revision_done
review_request
review_result
handoff
decision
state_update
artifact_created
final
error
```

## 5. watch / subscription

MetaGPT 中 Role 通过 watch 关注某些 Action 产生的消息。

当前项目中实现：

```text
AgentSubscription
```

每个 Agent 可以订阅特定 message_type、cause_by、from_agent。

例如：

```text
CoderAgent 订阅：delegation、critique、revision_request
ReviewerAgent 订阅：review_request、artifact_created、revision_done
PlannerAgent 订阅：user_request、review_result、error、blocking_issue
TesterAgent 订阅：test_request、revision_done
```

## 6. msg_buffer / inbox

MetaGPT 中每个 Role 有 msg_buffer。

当前项目中实现：

```text
AgentInbox
```

每个 Agent 每轮只读取自己 inbox 中相关消息，而不是看完整聊天记录。

这非常关键：不要让所有 Agent 都看到所有聊天记录，避免上下文污染。

------

# 三、目录结构要求

请在现有项目中新增以下目录，实际可根据项目已有结构微调，但要保持清晰：

```text
app/multiagent/
  __init__.py
  models.py
  messages.py
  state.py
  room.py
  bus.py
  inbox.py
  agent_spec.py
  runtime_adapter.py
  team_runner.py
  speaker_selector.py
  termination.py
  review_repair.py
  policies.py
  store.py
  prompts.py
  default_teams.py
```

可选新增：

```text
app/api/routes_team.py
app/cli/team.py
tests/test_multiagent_*.py
docs/multiagent_architecture.md
docs/multiagent_api.md
docs/multiagent_examples.md
```

------

# 四、核心模块设计

## 1. models.py

定义核心 Pydantic 模型或 dataclass：

```text
AgentSpec
TeamSpec
AgentMessage
AgentSubscription
AgentInboxItem
SharedTeamState
TeamDecision
TeamIssue
TeamArtifactRef
TeamRunConfig
TeamRunResult
```

TeamSpec 示例：

```json
{
  "name": "software_dev_team",
  "description": "面向软件开发任务的多智能体团队",
  "agents": [
    {
      "name": "PlannerAgent",
      "role": "任务规划者",
      "goal": "拆解用户目标，维护任务计划，协调协作节奏",
      "watched_message_types": ["user_request", "review_result", "error"],
      "allowed_tools": ["read_workspace", "write_artifact", "update_team_state"]
    },
    {
      "name": "CoderAgent",
      "role": "代码实现者",
      "goal": "基于计划完成代码实现和修复",
      "watched_message_types": ["delegation", "critique", "revision_plan"],
      "allowed_tools": ["read_file", "write_file", "run_shell", "search_code"]
    },
    {
      "name": "ReviewerAgent",
      "role": "质量评审者",
      "goal": "评审方案、代码、测试和产物质量",
      "watched_message_types": ["review_request", "artifact_created", "revision_done"],
      "allowed_tools": ["read_file", "search_code", "write_review"]
    }
  ],
  "max_rounds": 20,
  "termination_policy": "review_passed_or_max_rounds"
}
```

## 2. bus.py

实现 MessageBus。

能力：

```text
publish(message)
broadcast(message)
direct_send(message)
route_to_subscribers(message)
get_room_messages(room_id)
get_agent_inbox(room_id, agent_name)
ack_message(message_id, agent_name)
```

路由规则：

```text
1. visibility=direct 时，只投递给 to_agent
2. visibility=broadcast 时，根据 watched_message_types / cause_by / from_agent 投递
3. visibility=system 时，投递给所有 Agent 或指定系统组件
4. 所有消息都写入 room transcript
5. 所有投递都写入 agent inbox
```

## 3. inbox.py

实现每个 Agent 的 inbox。

能力：

```text
list_unread(agent_name)
mark_read(message_id)
get_relevant_context(agent_name, max_items)
summarize_old_messages(agent_name)
```

要求：

```text
- Agent 不应默认读取全部 transcript
- 每个 Agent 只读取自己的 inbox + shared_state + 必要 artifacts
- 可以保留最近 N 条相关消息
- 旧消息可压缩为摘要
```

## 4. state.py

实现 SharedTeamState。

至少包含：

```text
goal
phase
plan
current_round
open_questions
issues
decisions
artifacts
completed_steps
blocked_steps
review_status
final_output
metadata
```

phase 至少包含：

```text
created
planning
discussing
executing
reviewing
repairing
finalizing
completed
failed
cancelled
```

提供方法：

```text
update_phase()
add_issue()
resolve_issue()
add_decision()
add_artifact()
mark_step_done()
to_prompt_context()
```

## 5. room.py

实现 TeamRoom。

职责：

```text
- 创建多智能体任务房间
- 初始化 TeamSpec
- 初始化 Agent inbox
- 初始化 shared_state
- 调用 MessageBus 分发用户初始任务
- 管理 room lifecycle
- 判断是否 idle / done
```

## 6. runtime_adapter.py

实现 AgentRuntimeAdapter。

重点：每个 TeamAgent 的内部执行仍然可以复用现有 DeepAgents 能力。

设计：

```text
AgentRuntimeAdapter.run(agent_spec, inbox_messages, shared_state, workspace, artifacts) -> list[AgentMessage]
```

内部流程：

```text
1. 根据 agent_spec 构造 system prompt
2. 读取该 Agent 的 inbox 消息
3. 读取 shared_state 摘要
4. 读取必要 artifacts
5. 调用现有 build_agent / create_deep_agent
6. 让 DeepAgent 执行本轮任务
7. 要求输出结构化结果
8. 解析为 AgentMessage / state updates / artifact updates
```

注意：

```text
- 不要让 DeepAgent 自由输出无法解析的大段文本后就结束
- 要求 Agent 输出 JSON 或可解析结构
- 对解析失败要有 fallback，把结果作为 observation 消息发送给 PlannerAgent 或 ReviewerAgent
```

## 7. speaker_selector.py

实现 SpeakerSelector。

不要完全依赖 LLM。先实现规则优先，再实现 LLM fallback。

规则优先：

```text
- 如果 phase=created，PlannerAgent 先说话
- 如果有未处理 critique，被 critique 的 Agent 优先响应
- 如果有 review_request，ReviewerAgent 优先
- 如果有 revision_plan，CoderAgent 优先
- 如果有 test_request，TesterAgent 优先
- 如果所有 issue resolved 且 review passed，Finalizer 或 PlannerAgent 输出 final
- 如果没有明确候选，用 LLM selector 判断
```

LLM fallback：

```text
输入：
- shared_state
- last_messages
- candidate_agents
- open_issues
- current_phase

输出：
{
  "next_speaker": "ReviewerAgent",
  "reason": "当前代码实现完成，需要质量评审"
}
```

## 8. termination.py

实现 TerminationChecker。

终止条件：

```text
- final message 已产生
- shared_state.phase=completed
- ReviewerAgent 明确 review_passed
- 所有 required_steps completed
- 无 open blocking issue
- 达到 max_rounds
- 连续 N 轮无有效状态变化
- 用户取消
- 异常失败
```

注意：达到 max_rounds 时不能假装成功，要输出 partial result 和未完成原因。

## 9. review_repair.py

实现 Review-Repair Loop。

逻辑：

```text
1. CoderAgent / ResearcherAgent / PlannerAgent 产生产物
2. 发送 review_request 给 ReviewerAgent
3. ReviewerAgent 输出 review_result
4. 如果 passed：
   - 更新 shared_state.review_status=passed
5. 如果 failed：
   - 生成 critique 消息
   - 投递给责任 Agent
   - 责任 Agent 输出 revision_plan
   - 执行修复
   - 再次 review
6. 最大返工次数可配置
```

Reviewer 输出必须结构化：

```json
{
  "passed": false,
  "issues": [
    {
      "severity": "high",
      "problem": "缺少消息订阅机制测试",
      "evidence": "tests 中没有覆盖 MessageBus route_to_subscribers",
      "suggestion": "补充 direct/broadcast/subscription 三类测试"
    }
  ],
  "required_fix_owner": "CoderAgent"
}
```

------

# 五、API 改造要求

保留现有单 Agent API，不要破坏。

新增多 Agent API，建议：

```text
POST /api/team-tasks
GET  /api/team-tasks/{task_id}
GET  /api/team-tasks/{task_id}/events
GET  /api/team-tasks/{task_id}/messages
GET  /api/team-tasks/{task_id}/state
GET  /api/team-tasks/{task_id}/agents
POST /api/team-tasks/{task_id}/messages
POST /api/team-tasks/{task_id}/cancel
```

POST /api/team-tasks 请求示例：

```json
{
  "goal": "分析当前项目并生成多智能体改造方案",
  "team": "software_dev_team",
  "mode": "controlled_group_chat",
  "max_rounds": 20,
  "review_required": true
}
```

返回：

```json
{
  "task_id": "task_xxx",
  "room_id": "room_xxx",
  "status": "running"
}
```

------

# 六、默认团队配置

请实现至少 2 套默认 TeamSpec。

## 1. software_dev_team

用于软件开发任务：

```text
PlannerAgent
ResearcherAgent
CoderAgent
ReviewerAgent
TesterAgent
FinalizerAgent
```

角色边界：

```text
PlannerAgent：
- 负责拆任务、维护计划、协调阶段
- 不能直接宣布代码质量通过
- 不能替代 Reviewer

ResearcherAgent：
- 负责阅读项目、检索信息、总结现状
- 不能直接改代码
- 输出必须包含证据

CoderAgent：
- 负责代码实现和修复
- 必须响应 critique
- 修改后必须发 review_request

ReviewerAgent：
- 负责质量评审
- 不能直接改代码
- critique 必须包含 evidence、severity、suggestion、owner

TesterAgent：
- 负责运行测试、补充测试建议
- 输出 test_result

FinalizerAgent：
- 负责最终总结
- 必须基于 shared_state、decisions、artifacts 输出
```

## 2. research_team

用于调研分析任务：

```text
PlannerAgent
ResearcherAgent
CriticAgent
SynthesizerAgent
FinalizerAgent
```

------

# 七、Prompt 设计要求

在 `app/multiagent/prompts.py` 中集中管理多 Agent prompt。

每个 Agent 的 prompt 必须包含：

```text
- 角色边界
- 能做什么
- 不能做什么
- 当前 shared_state
- 当前 inbox 消息
- 可用工具
- 输出格式要求
- 消息类型规范
```

输出格式建议：

```json
{
  "thought_summary": "简短说明本轮判断，不暴露冗长推理",
  "actions": [
    {
      "type": "send_message",
      "to_agent": "CoderAgent",
      "message_type": "delegation",
      "content": "请根据计划实现 MessageBus 的 broadcast/direct 路由。",
      "requires_response": true
    },
    {
      "type": "update_state",
      "patch": {
        "phase": "executing"
      }
    }
  ]
}
```

禁止 Agent 只输出：

```text
我会继续处理
我同意
看起来不错
下一步应该……
```

必须要求每轮输出至少产生一个有效动作：

```text
send_message / update_state / create_artifact / request_review / final / no_op_with_reason
```

------

# 八、LangGraph / DeepAgents / LangSmith 使用要求

## DeepAgents

继续作为每个 Agent 的复杂任务执行内核。

要求：

```text
- 不要废弃现有 build_agent
- AgentRuntimeAdapter 应优先复用当前 agent_factory
- 每个 Agent 可以拥有不同 system_prompt、tools、permissions、memory scope
```

## LangGraph

用于多智能体运行时的状态图或可恢复流程。

可以实现一个 team graph：

```text
init_room
  ↓
select_speaker
  ↓
run_agent
  ↓
route_messages
  ↓
update_state
  ↓
check_termination
  ├── continue → select_speaker
  └── end → finalize
```

要求：

```text
- 保留 checkpoint 能力
- 支持恢复 team task
- 支持流式事件
```

## LangSmith

如果项目中已有 LangSmith 或 tracing 配置，继续保留。

多 Agent 任务中要尽量记录：

```text
- room_id
- agent_name
- round
- message_type
- selected_speaker
- termination_reason
```

若 LangSmith 未配置，不要强制依赖云服务，必须支持本地无 LangSmith 运行。

------

# 九、持久化与兼容性

当前项目已有 task store、task messages、task events、artifacts。优先复用。

但多 Agent 需要新增持久化对象：

```text
team_rooms
team_agents
agent_messages
agent_inbox
team_state
team_decisions
team_issues
team_rounds
```

如果当前项目使用 SQLite，先兼容 SQLite。不要强制切 PostgreSQL。

要求：

```text
- 单 Agent 旧数据结构不破坏
- 新增表要有初始化逻辑
- 所有新增表要有最小 CRUD
- 多 Agent 任务也要能映射到原有 task_id，便于统一展示
```

------

# 十、前端 / 展示要求

如果当前项目前端较简单，本次不要求大改 UI，但后端数据必须支持未来展示：

```text
- 当前 TeamRoom 状态
- Agent 列表
- 每个 Agent 的消息
- Agent 之间的消息流
- SharedState
- open issues
- decisions
- artifacts
- review results
```

可以先在 API 中返回结构化数据。

如果已有任务消息展示页面，可以把 AgentMessage 转换成原有 TaskMessage 兼容展示，但不能丢失 from_agent、to_agent、message_type。

------

# 十一、测试要求

必须补充测试。至少包括：

```text
tests/test_multiagent_message_bus.py
tests/test_multiagent_inbox.py
tests/test_multiagent_state.py
tests/test_multiagent_speaker_selector.py
tests/test_multiagent_termination.py
tests/test_multiagent_review_repair.py
```

测试覆盖：

```text
1. direct message 只进入指定 Agent inbox
2. broadcast 根据 subscription 分发
3. Agent 只读取自己的 inbox
4. SharedTeamState 能更新 phase/issues/decisions/artifacts
5. SpeakerSelector 能按 critique/review_request/test_request 选择正确 Agent
6. TerminationChecker 能在 review_passed 或 max_rounds 时结束
7. Review-Repair Loop 能从 failed review 生成 critique 并投递给 owner
8. 原有单 Agent 任务不受影响
```

------

# 十二、文档要求

新增文档：

```text
docs/multiagent_architecture.md
docs/multiagent_api.md
docs/multiagent_examples.md
```

文档必须说明：

```text
1. 当前多智能体架构
2. 与 MetaGPT 思想的对应关系
3. TeamRoom / MessageBus / AgentInbox / SharedState / SpeakerSelector 的职责
4. 如何创建一个新的 Agent
5. 如何创建一个新的 TeamSpec
6. 如何运行多 Agent 任务
7. 如何查看消息、状态、产物
8. 当前限制和后续规划
```

------

# 十三、执行步骤

请按下面顺序执行，不要直接乱改：

## Step 1：项目理解

先阅读：

```text
README.md
app/core/agent_factory.py
app/task/*
app/api/*
app/agents/*
langgraph.json
pyproject.toml 或 requirements
```

总结当前单 Agent 架构、任务流、存储流、事件流、DeepAgents 接入点。

## Step 2：设计改造方案

先输出一份简短设计方案到：

```text
docs/multiagent_architecture.md
```

包含：

```text
- 当前架构
- 目标架构
- 新增模块
- 数据模型
- API
- 兼容策略
```

## Step 3：实现基础模型

实现：

```text
app/multiagent/models.py
app/multiagent/messages.py
app/multiagent/state.py
app/multiagent/agent_spec.py
```

## Step 4：实现 MessageBus / Inbox / TeamRoom

实现：

```text
app/multiagent/bus.py
app/multiagent/inbox.py
app/multiagent/room.py
```

## Step 5：实现持久化

实现：

```text
app/multiagent/store.py
```

并与现有 task store 做必要集成。

## Step 6：实现 AgentRuntimeAdapter

实现：

```text
app/multiagent/runtime_adapter.py
```

复用当前 DeepAgents build_agent 逻辑。

## Step 7：实现 SpeakerSelector / TerminationChecker / ReviewRepair

实现：

```text
app/multiagent/speaker_selector.py
app/multiagent/termination.py
app/multiagent/review_repair.py
```

## Step 8：实现 TeamRunner

实现：

```text
app/multiagent/team_runner.py
```

核心循环：

```text
create room
publish user_request
while not terminated:
    select next speaker
    load agent inbox + shared_state
    run agent
    parse actions
    publish messages
    update shared_state
    emit task events
    persist all changes
finalize
```

## Step 9：实现 API

新增：

```text
app/api/routes_team.py
```

并挂载到 FastAPI app。

## Step 10：测试和修复

运行现有测试和新增测试。

如果测试失败，必须修复，不要跳过。

## Step 11：示例任务

提供一个本地可运行示例：

```text
python -m app.cli.team "分析当前项目架构，并让多个 Agent 给出改造建议，Reviewer 负责评审，Finalizer 输出总结"
```

如果 CLI 当前结构不方便，就至少提供 API 示例和 docs 示例。

------

# 十四、质量要求

这次改造的成败标准：

```text
1. 不是简单 subagents 委派，而是真正有 AgentMessage / MessageBus / AgentInbox。
2. Agent 之间可以 direct message 和 broadcast。
3. Agent 可以根据 subscription/watch 接收消息。
4. 每个 Agent 有私有 inbox，不默认读取全量聊天记录。
5. 有 SharedTeamState，任务进度不只存在聊天文本里。
6. 有 SpeakerSelector，系统能决定下一轮谁行动。
7. 有 TerminationChecker，避免无限循环。
8. 有 Review-Repair Loop，支持评审和返工。
9. 现有单 Agent 功能不破坏。
10. 新增代码有测试和文档。
```

------

# 十五、重要约束

请严格遵守：

```text
- 不要推倒重写整个项目。
- 不要删除现有单 Agent 运行路径。
- 不要把多智能体做成简单串行流水线。
- 不要只在 DeepAgents subagents 上加几个角色就结束。
- 不要让所有 Agent 共用完整聊天记录。
- 不要没有终止条件。
- 不要没有测试就声称完成。
- 不要强制依赖外网服务。
- 不要强制要求 LangSmith 可用；LangSmith 应是可选观测能力。
- 不要破坏现有 API 兼容性。
```

------

# 十六、最终交付

完成后请输出：

```text
1. 改造摘要
2. 新增文件列表
3. 修改文件列表
4. 多智能体运行流程说明
5. 与 MetaGPT 思想的对应关系
6. API 使用方式
7. 测试结果
8. 当前限制
9. 下一步建议
```

最终目标不是做一个演示玩具，而是把当前项目升级成一个可继续演进的多智能体运行框架雏形。

请开始执行。