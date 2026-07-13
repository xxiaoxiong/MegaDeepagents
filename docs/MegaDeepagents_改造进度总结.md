# MegaDeepagents Agent Teams 改造 — 实施进度总结（更新：2026-07-13）

> 对应任务书：`docs/MegaDeepagents_Agent_Teams_改造任务书.md`

## 当前测试状态

- **468 tests passed**（全量测试，排除 1 个真实 LLM 测试需 API key）
- 单独验证 `test_multiagent_complex_task.py::test_software_dev_full_flow_real_llm` ✅ passed
- 4 warnings（仅 FastAPI `on_event` 弃用告警，不影响运行时）
- 改造前基线：425 passed in 1290s → 现在 468 passed in ~208s

## 各 Phase 交付

### Phase 0 — 仓库审查与基线测试 ✅
- 摸清主链 + 已有断点；建立 baseline 测试基线
- 输出 `docs/agent-team-baseline-audit.md`

### Phase A — 修复断链 ✅
- 修复 orchestrator 与 scheduler 接口断链
- `TeamRunResult` 增补 `total_rounds` / `termination_reason` 字段

### Phase B — 统一主链 ✅
- `TeamRuntimeFacade` 完成并设为 CLI/API 默认入口
- CLI `--legacy` 降级到旧 TeamRunner（DISCUSSION 模式）

### Phase C — 持续 Teammate ✅
- `AgentInstance` 生命周期状态机：CREATED → SPAWNING → IDLE → RUNNING → STOPPED/FAILED
- `AgentRegistry` 支持心跳租约 + 自动清理过期 lease
- `agent_id_override`（Phase G 恢复所需）

### Phase D — 共享任务板 ✅
- `TaskBoard` 原子 `claim/start/complete/fail` + `compare_and_set` 安全
- 20 coroutine 同时认领同一 task 只能成功一个

### Phase E — 真实并行 ✅
- `ParallelTeamScheduler`：asyncio.Semaphore 并发调度
- 两个 sleep(2) 并行耗时 ~2s 而非 4s
- 同一 Agent `max_concurrency=1` 串行验证

### Phase F — Mailbox 与治理 ✅
- Mailbox：inbox + 容量上限 + 反压丢弃最旧
- PolicyHook 治理钩子 + 黑名单 + 频率限制
- broadcast_run / broadcast_role / reply_to / thread_id
- snapshot/restore 快照持久化
- wake_idle_agents — Idle Agent 唤醒原语（Phase G 增强）
- team_runtime.send_message 接入 Mailbox

### Phase G — 恢复与产品化 ✅
**SQLite 表**（当前 schema v3，`app/multiagent/store.py`）：
- `agent_instances` — Agent 元数据
- `task_runs` — 任务执行记录
- `artifacts` — Artifact 持久化
- `permission_requests` — 人审请求
- `team_events` — 全量审计事件
- `mailbox_messages` — 邮箱消息持久化
- `schema_version` — 版本号（v3）

**`phase_g_store.AgentRunHistory`**：CRUD 接口，`@property conn` 动态获取连接，跨测试隔离。

**`ResumeCoordinator`**：
- 从持久化重建 Agent → IDLE 注入 registry
- SUCCEEDED Task 跳过（不重复执行）
- 失败/已停止 Agent 不重建
- `team_runtime.resume_run` 接入 ResumeCoordinator

**三项 polish 任务（原进度总结标记"待续"）均已完成**：
1. ✅ `_default_checkpoint_loader` — 真实 SqliteSaver 加载器
2. ✅ `RecordTelemetryEvent` — Orchestrator `_emit_event` 写入 team_events 表，全阶段过渡
3. ✅ Mailbox snapshot — `flush_to_db` / `restore_from_db` 双向 SQLite 持久化

## 新增测试覆盖

| 文件 | 测试数 | 关注点 |
|---|---|---|
| `test_parallel_scheduler.py` | 6 | 并行争抢 + 耗时验证 + 串行约束 + DAG 同步 |
| `test_mailbox.py` | 12 | 投递 + 取信 + 治理 + 广播 + 协商 + 快照 + 唤醒 |
| `test_phase_g_store.py` | 9 | AgentInstance/TaskRun/TeamEvent/Artifact/Permission |
| `test_resume_coordinator.py` | 5 | 恢复重建 + 跳过 succeeded + 不动 failed + 跳过 stopped |
| `test_phase_g_polish.py` | 5 | Mailbox roundtrip + checkpoint loader + telemetry |

## §23 必修问题修复状态

| # | 问题 | 状态 |
|---|---|---|
| 1 | 主链走 ParallelTeamScheduler 而非 _run_sync_fallback | ✅ 已修（orchestrator.py） |
| 2 | CLI workspace 真实传入 Executor | ✅ 已修（team_runtime.py） |
| 3 | API 与 CLI 统一走 Facade | ✅ 已修（routes_team.py） |
| 4 | AgentInstance 身份不丢失 | ✅ 已修（agent_id_override） |
| 5 | Artifact ID 必须来自真实 ArtifactStore | ✅ 已修（executor.py 移除伪 ID） |
| 6 | ArtifactStore 注册表持久化到 SQLite | ✅ 已修（artifact.py create→SQLite） |
| 7 | Artifact 扫描递归 | ✅ 已修（executor.py rglob） |
| 8 | Verifier 读取真实 Artifact 文件 | ✅ 已修（verifier.py _enrich_with_artifact_store） |
| 9 | Worker success≠Task SUCCEEDED | ✅ 已修（board.complete 合法状态） |
| 10 | LLMRubricVerifier 数据模型构造正确 | ✅ 已修（CriterionFailure 参数名） |
| 11 | _run_single 失败不返回 completed | ✅ 已修（orchestrator.py 225-228） |
| 12 | Scheduler 失败不返回 success | ✅ 已修（parallel_scheduler） |
| 13 | 文件工具绝对路径越权拒绝 | ✅ 已修（run_workspace.py） |
| 14 | execute 工具无约束 Shell | ✅ 已修（危险命令黑名单） |
| 15 | model_policy 影响模型选择 | ✅ 已修（llm_factory.build_model_for_policy） |
| 16 | CapabilityRegistry 指标真实更新 | ✅ 已修（parallel_scheduler 调 record_success/record_failure） |
| 17 | max_concurrency 真实生效 | ✅ 已修（ParallelTeamScheduler asyncio.Semaphore） |
| 18 | Task Budget 真实执行 | ⚠️ 任务书层 token budget 未在本次范围，由 settings 层控制 |
| 19 | WorkspacePolicy 真实执行 | ✅ 已修（executor 受限工具函数） |
| 20 | MemoryPolicy/ContextPolicy 不过数据模型 | ✅ 已修（layered_memory.py recall_from_store 修复） |

## §26 完成标准状态

| 维度 | 完成度 |
|---|---|
| **架构标准（15 项）** | **14/15** (93%) |
| **质量标准（10 项）** | **9/10** (90%) |
| **综合** | **23/25 (92%)** |

## 关键文件索引

```
app/multiagent/
├── parallel_scheduler.py        ← Phase E 真实并行 + CapabilityRegistry 指标
├── mailbox.py                   ← Phase F 邮箱 + 治理 + Idle Agent 唤醒
├── phase_g_store.py             ← Phase G 持久化 CRUD
├── resume_coordinator.py        ← Phase G 恢复协调器（真实 checkpoint 加载）
├── task_board.py                ← Phase D 共享任务板
├── agent_registry.py            ← Phase C 持续 Teammate
├── agent_instance.py            ← Phase C 状态机
├── orchestrator.py              ← 主链 ParallelTeamScheduler 接入 + 遥测事件
├── artifact.py                  ← Artifact SQLite 双写 + load_from_db 恢复
├── verifier.py                  ← ArtifactStore 接入 + CriterionFailure 修
├── team_runtime.py              ← 主入口，已接入 Mailbox + Resume + Facade
├── store.py                     ← Phase G 新表（schema v3）
└── executor.py                  ← model_policy 生效 + 递归扫描 + 伪 ID 移除

app/
├── llm_factory.py               ← build_model_for_policy 超参注入
├── api/routes_team.py           ← 统一走 TeamRuntimeFacade
└── cli.py                       ← 统一走 TeamRuntimeFacade
```
