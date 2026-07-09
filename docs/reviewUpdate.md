你现在需要对当前项目 `general-agent-frame` 做一次多智能体架构审查与优化。

当前项目是一个基于 DeepAgents / LangChain / LangGraph 的单智能体复杂任务运行框架，已经具备一定的任务执行、工具调用、workspace、memory、skills、permissions、HITL、task events、task messages、artifacts 等能力。现在目标不是简单增加几个 subagents，而是判断它是否具备升级为“真正多智能体运行框架”的基础，并给出架构级优化方案。

请你以资深 AI Agent 架构师的视角，完整阅读项目代码，重点审查当前项目距离真正 Multi-Agent Runtime 还缺哪些关键模块，并在不破坏现有单 Agent 能力的前提下，提出分阶段优化方案。

------

# 一、核心判断标准

真正的多 Agent 不是：

```text
多个 subagent 串行执行
多个角色 prompt 并行跑完后汇总
一个主 Agent 调用几个专家工具
多个 Agent 共用完整聊天记录
```

真正的多 Agent 应该具备：

```text
多个 Agent 有明确职责边界
Agent 之间通过结构化消息通信
每个 Agent 有自己的 inbox / memory / tools / permissions
Agent 可以 direct message / broadcast / handoff
系统有共享任务状态 SharedState
系统有消息路由 MessageBus
系统有发言调度 SpeakerSelector
系统有评审返工 Review-Repair Loop
系统有终止条件 TerminationChecker
所有过程可观测、可审计、可回放
```

你的审查目标就是判断当前项目在这些能力上分别处于什么状态。

------

# 二、请先完整阅读这些文件和目录

请优先阅读：

```text
README.md
pyproject.toml / requirements.txt
langgraph.json
app/core/agent_factory.py
app/task/
app/api/
app/agents/
app/tools/
app/memory/
app/skills/
app/workspace/
```

重点理解：

```text
1. 当前单 Agent 是如何创建的
2. DeepAgents 是如何接入的
3. 任务是如何启动、运行、暂停、恢复、结束的
4. task events / task messages / artifacts 是如何记录的
5. subagents 当前是如何配置和调用的
6. LangGraph / checkpointer / memory / permissions 是否已经有基础能力
7. 当前代码中是否已有可复用为多 Agent runtime 的模块
```

------

# 三、按多 Agent 独特模块逐项审查

请基于下面每个模块做审查，输出“当前状态、存在问题、优化建议、是否需要新增代码”。

## 1. TeamRoom / Environment

审查当前项目是否有类似“多 Agent 任务房间”的概念。

判断标准：

```text
是否有一个多 Agent 任务环境？
是否能管理多个 Agent？
是否能维护团队共享状态？
是否能维护团队消息流？
是否能维护团队产物、决策、问题和阶段？
```

如果没有，请设计：

```text
TeamRoom
room_id
task_id
participants
shared_state
message_bus
agent_inboxes
artifacts
decisions
issues
lifecycle
```

## 2. AgentSpec / Role Definition

审查当前项目是否有明确的 Agent 角色定义。

判断标准：

```text
每个 Agent 是否有 name / role / goal？
是否有明确职责边界？
是否定义了能做什么和不能做什么？
是否有 allowed_tools？
是否有 watched_message_types？
是否有权限边界？
```

重点避免：

```text
只用 system prompt 区分角色
Reviewer 可以写代码
Coder 可以宣布评审通过
Planner 可以直接修改实现
所有 Agent 权限一样
```

## 3. AgentMessage / 结构化消息协议

审查当前 task messages 是否足以支持 Agent 间通信。

判断标准：

```text
消息里是否包含 from_agent？
是否包含 to_agent？
是否包含 message_type？
是否支持 direct / broadcast？
是否支持 reply_to / thread_id？
是否支持 requires_response？
是否支持 evidence / artifact_refs？
是否支持 status / handled / ack？
```

如果当前消息只是 role/content/extra，请提出 AgentMessage 设计。

推荐消息类型：

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

## 4. MessageBus / 消息总线

审查当前项目是否有消息路由能力。

判断标准：

```text
是否支持 Agent A 给 Agent B 发 direct message？
是否支持 broadcast？
是否支持基于 message_type 的订阅？
是否能把消息投递到指定 Agent inbox？
是否能记录完整 transcript？
是否能判断消息是否被处理？
```

如果没有，请设计 MessageBus：

```text
publish()
broadcast()
direct_send()
route_to_subscribers()
get_room_messages()
get_agent_inbox()
ack_message()
```

## 5. AgentInbox / 私有收件箱

审查当前项目是否让所有 Agent 共用完整上下文。

判断标准：

```text
每个 Agent 是否有自己的 inbox？
Agent 是否只读取与自己相关的消息？
旧消息是否能摘要压缩？
是否避免所有 Agent 看到所有聊天记录？
```

如果没有，请设计 AgentInbox。

目标：

```text
PlannerAgent 只看规划相关消息
CoderAgent 只看 delegation / critique / revision_request
ReviewerAgent 只看 review_request / artifact_created / revision_done
TesterAgent 只看 test_request / revision_done
```

## 6. SharedState / Blackboard

审查当前项目是否有外置共享状态，而不是只依赖聊天上下文。

判断标准：

```text
是否记录 goal？
是否记录 phase？
是否记录 plan？
是否记录 open issues？
是否记录 decisions？
是否记录 artifacts？
是否记录 review_status？
是否记录 completed_steps？
是否记录 blocking_steps？
```

如果没有，请设计 SharedTeamState。

推荐字段：

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

## 7. SpeakerSelector / 发言调度器

审查当前项目是否能判断下一轮由哪个 Agent 行动。

判断标准：

```text
是否只是固定串行？
是否所有 Agent 都能随便行动？
是否能根据 critique / review_request / test_request 选择下一个 Agent？
是否有规则优先 + LLM fallback？
```

推荐规则：

```text
没有计划 → PlannerAgent
有 delegation → 对应 owner Agent
有 critique → 被 critique 的 Agent
有 review_request → ReviewerAgent
有 test_request → TesterAgent
review passed → FinalizerAgent
出现 error → PlannerAgent 或 RecoveryAgent
```

## 8. Orchestrator / TeamRunner

审查当前 TaskRunner 是否可以扩展为 TeamRunner。

TeamRunner 应该控制：

```text
create room
publish user_request
select next speaker
load agent inbox
load shared_state
run agent
parse actions
publish messages
update shared_state
persist events
check termination
finalize
```

请判断当前 TaskRunner 哪些能力可以复用，哪些需要新增。

## 9. Tool Permission / Agent-specific Tools

审查当前工具权限是否能按 Agent 隔离。

判断标准：

```text
是否所有 Agent 共用同一批工具？
是否支持 Agent 级 allowed_tools？
是否支持 Reviewer 只读、Coder 可写、Tester 可运行测试？
是否支持高风险工具 HITL？
```

推荐：

```text
PlannerAgent：计划、状态、文档，不写业务代码
CoderAgent：读写文件、运行命令、修改实现
ReviewerAgent：读代码、写 review，不直接改代码
TesterAgent：运行测试、写测试结果
FinalizerAgent：汇总，不修改代码
```

## 10. Review-Repair Loop

审查当前项目是否有真正评审返工闭环。

判断标准：

```text
是否有 review_request？
是否有 critique？
是否有 revision_plan？
是否有 revision_done？
是否能 failed review 后自动回到责任 Agent？
是否能复审？
是否有最大返工次数？
```

如果没有，请设计。

## 11. Conflict Resolution / 冲突处理

审查当前项目是否能处理多 Agent 意见冲突。

判断标准：

```text
Planner / Coder / Reviewer 意见不一致时谁裁决？
安全问题和功能开发冲突时谁优先？
是否支持 Supervisor 裁决？
是否支持 HITL？
```

建议策略：

```text
规则优先
Reviewer 对质量问题有否决权
Supervisor / Planner 负责流程裁决
高风险操作交给 HITL
```

## 12. TerminationChecker / 终止条件

审查当前项目是否能防止多 Agent 无限循环或提前结束。

判断标准：

```text
是否所有 required steps 完成？
是否所有 open issues 关闭？
是否 review_status=passed？
是否没有未处理 requires_response 消息？
是否达到 max_rounds？
是否连续 N 轮无有效状态变化？
```

达到 max_rounds 时不能假装成功，要输出 partial result。

## 13. Artifact Ownership / 产物归属

审查当前 artifacts 是否能支持多 Agent 协作。

判断标准：

```text
artifact 是否记录 created_by？
是否记录 updated_by？
是否记录 reviewed_by？
是否记录 status？
是否关联 message_id？
是否有 version？
是否能知道哪个 Agent 对哪个产物负责？
```

## 14. Multi-level Memory / 记忆分层

审查当前 memory 是否能支持多 Agent。

判断标准：

```text
是否只有全局 memory？
是否支持 Agent private memory？
是否支持 team shared memory？
是否支持 task episodic memory？
是否支持 decision memory？
```

建议分层：

```text
AgentPrivateMemory
TeamSharedMemory
TaskEpisodicMemory
DecisionMemory
ArtifactMemory
```

## 15. Observability / 可观测与回放

审查当前 logs/events 是否足以 debug 多 Agent。

必须能观察：

```text
每一轮 selected_speaker
为什么选择该 Agent
该 Agent 看到哪些 inbox messages
该 Agent 输出哪些 actions
产生了哪些 AgentMessages
状态发生了什么 diff
调用了哪些 tools
产物如何变化
为什么终止
```

如果已有 LangSmith / tracing，请判断如何保留并增强；如果没有，不要强制依赖外网服务。

------

# 四、请给出审查结果矩阵

请输出一个表格：

```text
模块
当前是否具备
成熟度评分 0-10
主要问题
优化优先级 P0/P1/P2
建议实现方式
涉及文件
```

评分参考：

```text
0 = 完全没有
3 = 有雏形但不可用
5 = 可复用部分能力
7 = 基本可用但不完整
10 = 已经成熟
```

------

# 五、请给出分阶段优化路线

不要一次性大改。请按阶段给出路线。

## Phase 1：多 Agent 最小通信内核

目标：先让系统具备真正 Agent 间通信能力。

必须包含：

```text
AgentSpec
AgentMessage
MessageBus
AgentInbox
SharedTeamState
TeamRoom
```

## Phase 2：团队运行循环

目标：让多 Agent 能围绕任务跑起来。

必须包含：

```text
TeamRunner
SpeakerSelector
AgentRuntimeAdapter
TerminationChecker
基础 API
```

## Phase 3：质量闭环

目标：让多 Agent 不是聊天，而是能交付。

必须包含：

```text
Review-Repair Loop
Artifact Ownership
Issue Tracking
Decision Tracking
```

## Phase 4：工程化增强

目标：让系统可审计、可恢复、可扩展。

必须包含：

```text
LangGraph checkpoint
LangSmith / local tracing
SSE / WebSocket events
Agent-specific permissions
memory 分层
HITL
测试覆盖
```

------

# 六、请给出必要代码优化建议

请不要直接大规模重写。先识别最小修改点。

请输出：

```text
新增哪些目录
新增哪些核心文件
修改哪些已有文件
哪些旧逻辑保持不变
哪些旧逻辑需要适配
哪些风险最高
```

建议新增目录：

```text
app/multiagent/
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
  store.py
  prompts.py
  default_teams.py
```

建议新增测试：

```text
tests/test_multiagent_message_bus.py
tests/test_multiagent_inbox.py
tests/test_multiagent_state.py
tests/test_multiagent_speaker_selector.py
tests/test_multiagent_termination.py
tests/test_multiagent_review_repair.py
```

------

# 七、请给出一个推荐目标架构

请基于当前项目实际代码，画出目标架构：

```text
FastAPI / CLI / Web
  ↓
TaskService
  ↓
TeamRunner
  ↓
TeamRoom
  ├── MessageBus
  ├── AgentInbox
  ├── SharedTeamState
  ├── SpeakerSelector
  ├── TerminationChecker
  └── ArtifactStore
       ↓
AgentRuntimeAdapter
  ├── PlannerAgent    -> DeepAgent
  ├── ResearcherAgent -> DeepAgent
  ├── CoderAgent      -> DeepAgent
  ├── ReviewerAgent   -> DeepAgent
  └── TesterAgent     -> DeepAgent
```

并说明：

```text
DeepAgents 继续负责单 Agent 深度执行
LangGraph 负责状态图、checkpoint、可恢复流程
LangChain 负责模型、工具、消息基础能力
LangSmith 可选负责 tracing
MetaGPT 思想用于 TeamRoom / MessageBus / Role.watch / AgentInbox / SOP
当前项目平台层继续负责 API、任务、权限、产物、事件、HITL
```

------

# 八、请输出最终审查报告

请生成：

```text
docs/multiagent_review_report.md
```

报告必须包含：

```text
1. 当前项目单 Agent 架构总结
2. 当前项目已有可复用能力
3. 当前项目距离多 Agent Runtime 的差距
4. 15 个核心模块逐项评分
5. 最小可行改造方案
6. 分阶段改造路线
7. 推荐目录结构
8. 推荐数据模型
9. 推荐 API
10. 风险点
11. 测试建议
12. 是否建议在当前项目上继续改造
```

最后明确给出结论：

```text
建议继续基于当前项目改造 / 建议部分重构 / 建议重做
```

并说明理由。

------

# 九、执行约束

请严格遵守：

```text
不要删除现有单 Agent 能力
不要破坏现有 API
不要直接引入 MetaGPT 作为硬依赖
不要只增加 DeepAgents subagents 就结束
不要把多 Agent 做成简单串行流水线
不要让所有 Agent 共享完整聊天记录
不要强制依赖外网 LangSmith
不要跳过测试设计
不要只写空泛建议，必须结合当前项目文件和代码
```

------

# 十、最终交付格式

完成后请输出：

```text
1. 审查摘要
2. 当前项目能否升级为多 Agent 的判断
3. 关键差距列表
4. 优先级最高的 P0 改造项
5. 已生成的 docs/multiagent_review_report.md 路径
6. 建议下一步执行的具体任务清单
```

请开始审查。