# Agent Team Parity Audit

## 审计范围与基线

- 基线分支：`main`
- 审计时基线提交：`9aa7886`（合并 PR #5）
- 工作分支：`agent/claude-agent-team-runtime-v2`
- 已检查生产入口、Orchestrator、并行 Scheduler、Executor、TeamBuilder、TaskGraph、
  TaskBoard、AgentInstance/Registry/RuntimeManager、Mailbox、Artifact、Verifier、
  ResumeCoordinator、API、Planner、Phase G SQLite 表和相关测试。
- 审计前可见远端 PR 为 #1–#5，最新 #5 已合并；未发现比 `main` 更新且需移植的开放修复。

本文记录的是重构后的事实边界，并保留审计中发现的旧行为，避免文档继续描述另一套不存在
的运行时。

## 当前真实生产主链

生产团队入口统一为：

```text
API / CLI
  → TeamRuntimeFacade (TASK_TEAM)
  → SimpleOrchestrator
  → TransactionalTaskService
  → ParallelTeamScheduler
  → TeammateSupervisor / TeammateSession
  → DeepAgentExecutor
  → ArtifactStore / Verifier
  → AgentWorktreeManager / GitIntegrationManager
```

`TeamRuntimeFacade` 创建 `TeamRunContext`、持久化 run 元数据，并注入同一套 Executor、
ArtifactStore、Verifier、PermissionBroker 和 Git runtime。API 默认不直接实例化 Scheduler、
Executor 或 TeamRunner。

## Legacy 主链

`DISCUSSION → TeamRunner → SpeakerSelector → AgentRuntimeAdapter → Action JSON` 是 Legacy。
它继续服务显式讨论模式和旧测试，但不获得 Teammate Session、团队工具、Artifact 依赖、
worktree、PermissionBroker、Plan Approval、动态团队或 v2 恢复能力。

旧 `TaskScheduler/_InMemoryWorkerExecutor` 仅用于无 `TeamRunContext` 的测试/兼容路径；
TASK_TEAM 生产 run 必须经过 `ParallelTeamScheduler`。不得让伪 Artifact 或固定成功 Executor
进入 Facade。

## 状态事实边界

### 审计前风险

TaskGraph 和 TaskBoard 都能写任务状态，Orchestrator 通过双向同步覆盖两边；动态 Repair
直接修改内存图后再补 Board，崩溃点会产生漂移。历史 `task_runs=succeeded` 还可能在恢复时
把未验证 Board 任务直接晋升成功。

### 当前边界

- `TaskGraph`：计划结构、依赖、OutputContract、预算、版本和 Mutation 历史。
- `TaskBoard`：SQLite 中的运行态权威源，保存认领、所有者、尝试、错误、Produced、
  Verifying、Succeeded、Repair/Blocked/Cancelled。
- 初始计划、动态创建、依赖修改和 Repair 都通过 `TransactionalTaskService`，在
  `BEGIN IMMEDIATE` 内写 graph snapshot、Board row、幂等 mutation 和 outbox。
- `mutation_id` 重放返回已有版本；运行中的整图不能覆盖 Board 状态。
- Graph ← Board 仅作为只读/结果投影；历史 `task_runs` 不能晋升 Board 状态。
- TaskBoard claim 使用 SQLite `BEGIN IMMEDIATE`，不同 Board 实例只会有一个认领者。

## Agent 与 Teammate 生命周期

`AgentInstance` 是控制平面身份；`TeammateSession` 是稳定执行身份。映射保留：

- run/profile/agent/session/thread/checkpoint namespace
- current task 与对话状态
- workspace/worktree、inbox、mailbox cursor
- command/event queue、permission request IDs
- current tool call、cancel 状态和最后活动时间

Teammate 生命周期为：

```text
CREATED → SPAWNING → IDLE → CLAIMING → PLANNING
  → WAITING_PLAN_APPROVAL → RUNNING ↔ WAITING_TOOL
  → WAITING_PERMISSION / BLOCKED → IDLE
  → STOPPING → STOPPED
任意受支持状态 → FAILED（可治理恢复）
```

任务完成后 Session 回到 `IDLE`；同一 Agent 的后续任务保留 session/thread/checkpoint 和
worktree。Supervisor 从 `teammate_sessions` 恢复相同身份，命令/事件队列在
`teammate_queue_items` 中按 sequence 持久化。执行期间的用户或队友消息进入命令队列，
并在每个工具安全点被 Actor 消费到同一 conversation state。

## Task 生命周期

```text
PENDING → CLAIMED → RUNNING → PRODUCED → VERIFYING → SUCCEEDED
                         ├→ REPAIR_REQUIRED
                         ├→ REPLAN_REQUIRED / BLOCKED
                         ├→ PENDING（安全释放/重试）
                         └→ FAILED / CANCELLED
```

Worker 只调用 `mark_produced`。只有 Verifier 与完成 Hook 通过，且 Git 集成无冲突后，
Scheduler 才能调用 `mark_verified`。异常、迟到结果、模型不可用、文件非空、缺少证据、
集成测试失败或待处理高风险权限都不能完成 run。

## Artifact 流向

1. Executor 扫描真实变更文件，在 run 的 `artifacts/` 空间创建真实 Artifact。
2. Artifact 包含 ID、run/task/agent、类型、相对路径、SHA-256、版本、Commit SHA、状态、
   时间和 predecessor/parent lineage。
3. Worker 返回真实 ID；Board 进入 `PRODUCED`。
4. Verifier 读取特定 Task 的真实文件和元数据，验证通过后 Artifact 进入 `VERIFIED`。
5. 下游被调度时只收集直接依赖、同 run、已验证、文件存在且哈希一致的 Artifact，传入
   ID、路径、摘要、hash、version、Commit SHA 和 producing agent。
6. 文件缺失、哈希变化、run 不匹配或未验证会 fail-closed。
7. Repair Mutation 携带源 Artifact IDs、结构化 failed criteria/evidence/affected files 和
   Verifier 反馈；lineage API 可跨恢复查询。

## Mailbox 流向

```text
user / Agent
  → TeamRuntimeFacade 或 TeamControlPlaneService
  → Mailbox SQLite（先持久化）
  → target TeammateCommandQueue
  → Actor safety_point
  → Session inbox + conversation state
```

消息校验 caller 的 run/agent 身份，不能冒充用户或其他 Agent。损坏 SQLite 行逐条跳过，
不阻塞合法消息；consumed 状态防止恢复后重复投递。SSE 使用统一 envelope 和单调 sequence，
客户端可按 `after_sequence` 补拉。

## 团队协作 Control Plane

DeepAgent 获得 19 个绑定 caller 的内部工具：成员与任务查询、原子认领、Task Mutation、
Blocked/Replan、direct/broadcast/read/wait 消息、动态 Teammate、shutdown 请求、权限请求、
Plan 提交和进度上报。

工具不持有可直接修改的 TaskGraph/TaskBoard 引用；所有 mutation 经过 Control Plane，写审计
事件并校验 run、agent、能力、依赖、状态和权限。Agent 工具没有设置 `SUCCEEDED` 的入口。

## Git workspace 与集成

Git run 记录源仓库、base branch 和 base SHA，目录为：

```text
run_workspace/
  control/
  artifacts/
  worktrees/<agent_id>/
  integration/repo/
  logs/
```

- 每个 Agent 使用独立 branch/worktree，后续任务按持久化租约复用。
- Branch/commit/push 必须经过 PermissionBroker；Worker 不能修改主 worktree 或 push
  `main/master`。
- 任务 Artifact 绑定 commit SHA；Integration Manager 在独立 integration branch 合并。
- 相同文件的并行提交产生真实 Git conflict，记录冲突文件并进入 Repair，不做最后写覆盖。
- dirty 或存在 base 之后 commit 的 worktree 不会被静默删除；进程恢复读取原租约。
- Run 只有在配置并通过 integration test argv 后才能在 Git 模式完成。
- `LocalWorkspaceProvider` 仅用于明确的非 Git run，不改变 Git 编码 run 的安全模型。

## Verifier

生产 Facade 使用 `LLMRubricVerifier(model_available=True, fail_closed=True)`。模型调用失败时
回退只会识别明显不完整，不能因为 Artifact 非空而 PASS。

`OutputContract → VerificationPlan` 支持 required files/hash、JSON Schema、输出格式、test、
lint、type-check、build、security、通用结构化 argv、acceptance criteria、semantic rubric、
diff scope 和 forbidden changes。命令证据记录 argv、return code、stdout、stderr、cwd、平台和
耗时。无 semantic rubric 时，真实通过的 test/build/hash/schema 可以作为完成证据；仅文件
存在或非空不算正确性证据。

失败结果包含 failed criterion、evidence、severity、recommended fix、affected files 和
proposed tasks，并写入 Board 的 `verification` 元数据供 Repair 使用。

## 取消、工具与权限边界

- 所有本地工具在开始前运行 safety point 和 CancellationToken 检查。
- Shell 使用结构化 argv、`shell=False`、分类策略、超时、输出上限和独立进程组；运行中取消
  先 TERM，超时后 KILL。
- Unix、Windows `cmd`、PowerShell 分别识别只读、复合脚本和危险边界；未知命令默认申请
  权限或拒绝。
- 文件与 Artifact 通过同目录临时文件、fsync、`os.replace` 原子发布，拒绝 `..`、绝对路径
  和 symlink 逃逸。
- ToolSideEffectJournal 使用 idempotency key 记录 before/completed/failed；恢复时未完成的
  side effect 标为需要人工判断，不盲目重放。
- AgentProfile 是第一层静态权限；PermissionPolicy/Broker 是参数级第二层。支持 approve once、
  approve for run、deny、deny with feedback。决定记录操作者、原因、时间、范围；Agent 和
  其他 Agent 消息不能批准。

## Plan Approval、Lead、动态团队与 Hook

- 高风险 Task 可要求 Teammate 先提交文件范围、步骤、测试、风险和回滚计划；批准前任务保持
  `WAITING_PLAN_APPROVAL/BLOCKED`。低风险规则可由 Lead 自动批准，高风险进入用户 HITL。
- `LeadCoordinatorAgent` 只观察并提出建议；所有实际操作仍走 Control Plane。
- DynamicTeamManager 按能力创建稳定 Session，执行 team size、spawn depth、agents/run、
  concurrency、token/cost/tool-call 预算；子 Agent 工具权限是父 Agent 的交集。找不到能力时
  明确失败，不回退高权限 Coder。
- LifecycleHookEngine 覆盖 Run、Task、Tool、Permission、Teammate、Message、Verification
  生命周期，支持 system/user/project scope、timeout、fail-open/fail-closed、block、feedback、
  metadata、human 和 replan。TaskCompleted Hook 可把任务退回 Repair。

## 恢复语义

恢复顺序为：Board → interrupted task 安全重入队 → Mailbox → Tool side-effect journal →
AgentInstance → 同一 TeammateSession → checkpoint → ArtifactStore → pending permission/plan →
Scheduler。

恢复不会根据历史 worker task_run 伪造成功。已验证 Board 行保持 SUCCEEDED；RUNNING/CLAIMED
在死进程租约释放后回到 PENDING；approved permission/plan 只解阻对应 Task。Agent ID、Session、
Thread、checkpoint namespace、worktree 和 mailbox cursor 保持不变。

## API 与事件协议

`/team-runs` 提供 run、Teammate、TaskGraph、TaskBoard、Artifact/lineage、Mailbox、peek、
transcript、attach/detach、pause/resume/stop/cancel、permission、plan、worktree/Git、verification、
error/Repair 和 replayable SSE。

Envelope 字段为 `event_id/run_id/agent_id/task_id/event_type/sequence/timestamp/payload/trace_id`。
`sequence` 在同一个 run 内单调递增。

## 数据库迁移

Schema version 从 3 升至 4。迁移完全 additive，功能模块首次使用时以
`CREATE TABLE IF NOT EXISTS` 创建：

- `teammate_sessions`, `teammate_queue_items`
- `structured_permission_requests`, `teammate_plans`
- `task_graph_mutations`, `control_plane_outbox`
- `worktree_leases`, `merge_queue`, `parent_child_agent_links`
- `tool_invocations`, `event_envelopes`

原有 `team_runs`、`task_graph_snapshots`、`task_board_tasks`、`agent_instances`、`task_runs`、
`artifacts` 和 `mailbox_messages` 原地保留，无破坏性数据重写。回滚代码前应保留 v4 表，而不是
删除恢复/审计数据。

## 测试覆盖

新增 `tests/test_agent_team_runtime_v2.py`，覆盖：

- 同一 Session 连续任务、IDLE、消息安全点和 Supervisor 重启恢复
- 不同 TaskBoard 实例并发认领唯一获胜
- Mutation 幂等与 DAG 环拒绝
- 权限不可自批、run grant 跨 Broker 重建
- Shell 注入回归、运行中取消、Unix/cmd/PowerShell 分类
- verified Artifact 依赖流、文件缺失与 hash 篡改拒绝
- 非空错误文件在 LLM 不可用时不能通过
- 临时真实 Git 仓库的独立 worktree、不同文件集成、同文件冲突、主工作区隔离、保护分支、
  dirty/ahead worktree 保留、租约恢复和显式 gitignored 环境文件白名单
- 动态团队预算、嵌套深度/规模和父子工具权限子集
- 无能力匹配时不回退 DefaultCoder

Live model/real LangSmith 测试保留，但默认 CI 跳过；设置 `RUN_LIVE_MODEL_TESTS=1` 后显式运行。

## 审计发现并已修复的 Bug

1. Scheduler 以“全部 produced”或 worker success 近似完成。
2. LLM 不可用 + 非空文件会乐观 PASS。
3. Resume 根据历史 task_run 直接晋升 Board 成功。
4. 能力不匹配回退高权限 DefaultCoder。
5. ArtifactStore 为 Verifier 注入同 run 的无关任务 Artifact。
6. Scheduler 并发信号量只覆盖 Agent 选择，没有覆盖整个执行生命周期。
7. TaskBoard claim 只有单进程锁，没有 SQLite 级原子竞争。
8. Plan Approval 恢复分支被 permission 分支提前 `continue`，无法解阻。
9. 工具只在 invoke 前后取消，长 Shell 和文件写缺少安全点/原子发布。
10. Shell 黑名单和字符串命令不能抵抗操作符绕过。
11. Repair 直接修改内存 Graph，未与 Board/outbox 原子提交。
12. API/文档把 Legacy TeamRunner 描述成默认生产架构。

## 已知真实限制

- 真实模型 E2E 需要外部模型凭证；默认 CI 只运行 deterministic/offline suite。
- 远端 push 和 Draft PR 创建需要 Git remote 凭证及 `gh`/GitHub connector。当前代码已准备
  分支、commit/integration/PR 元数据接口，但无凭证时不会伪造 PR 地址。
- SQLite 适合单机多线程/多进程；跨主机部署仍需把事务服务和队列替换为共享数据库/消息系统。
- Legacy DISCUSSION 代码仍存在以保持 API 兼容，后续只能删除，不能继续分叉新能力。

## 后续决策

1. 所有新多 Agent 功能只进入 TASK_TEAM Facade。
2. 保持 TaskBoard 运行权威、Graph 计划权威，Repair/Replan 统一 Mutation。
3. Git coding run 默认 fail-closed；无 broker、无验证命令、无集成证据都不完成。
4. 下一阶段在具备 GitHub 凭证的环境完成远端 Draft PR，并用可选 live suite 验证真实模型，
   不在无凭证 CI 中引入假成功。
