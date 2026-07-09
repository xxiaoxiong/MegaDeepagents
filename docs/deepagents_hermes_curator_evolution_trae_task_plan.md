# 通用智能体框架 自进化版：Trae 实现任务清单

## 0. 任务目标

请在上一版基础上继续升级，目标是实现一个自主任务型智能体框架，具备 Skill 生命周期治理、自我改进提议和安全回滚能力。

上一版重点是：

```text
能接任务 → 能规划 → 能调用工具 → 能生成产物 → 能记忆 → 能审批
```

本次升级重点是：

```text
能长期使用 → 能治理 Skill 膨胀 → 能区分用户资产和 Agent 自动产物 → 能安全地产生自我改进建议 → 能评估优化效果 → 能回滚
```

最终交付物不是 Claude Code，也不是 IDE 代码助手，而是一个具备以下能力的自主任务型 Agent：

1. 用户给任务，Agent 自主执行并交付结果；
2. Agent 可以沉淀经验为 Skill 或 Memory；
3. 系统可以定期整理 Agent 自己生成的 Skill；
4. 用户手写或手动修改的 Skill 不会被后台整理器误伤；
5. 后台自我改进默认只生成提议，不自动覆盖；
6. 所有后台整理都有快照、报告、可恢复路径；
7. Prompt / Skill 的优化必须经过评测集验证，不允许只靠 LLM 自评。

---

## 1. 核心定位

系统定位：

```text
通用智能体框架（General Agent Framework）
```

也可以理解为：

```text
一个具备 Skill 生命周期治理、自我改进提议、历史检索、任务执行和安全回滚能力的 Agent Harness 原型。
```

---

## 2. 本次升级相对 Hermes-Lite 的新增能力

| 模块 | Hermes-Lite 已有 | 本次升级目标 |
|---|---|---|
| 任务执行 | CLI / API / 极简 Web 任务台 | 增加任务评测、任务结果评分、产物质量记录 |
| Skill | 能加载和读取 Skill | 增加 Skill 元数据、血统、状态、使用统计、归档、恢复 |
| Memory | 热记忆 + 冷记忆搜索 | 增加 Memory Curator、敏感信息过滤、记忆更新提议 |
| Nudge | 生成 review proposal | 升级为 Nudge → Curator → Evaluation 的闭环 |
| Curator | 无 | 新增 idle-triggered Curator 双阶段机制 |
| Provenance | 无 | 新增 created_by / source / pinned / bundled / hub-installed |
| 自进化 | 只有安全版提议 | 新增可评测的 Prompt / Skill 优化管线 |
| 回滚 | 基础 review queue | 增加 snapshot、archive、restore、diff、audit report |
| 可观测性 | 任务记录 | 增加 curator report、evolution run、skill health dashboard |
| 前端 | 极简任务台 | 增加 Skill 管理、Curator 状态、Review Queue、Diff 审批页 |

---

## 3. 重要设计原则

### 3.1 永远不直接删除

任何后台整理动作都不能删除 Skill。

允许的最大破坏性动作是：

```text
archive：移动到 runtime/skills/.archive/
```

并且必须可恢复。

### 3.2 用户资产优先保护

用户手写、用户手动修改、用户 pin 的 Skill 不允许被 Curator 自动整理。

默认规则：

```text
created_by=user        → Curator 不动
created_by=agent       → Curator 可整理
created_by=system      → Curator 不动
created_by=hub         → Curator 不动
pinned=true            → Curator 不动
```

### 3.3 后台任务默认只生成提议

Nudge、Curator、Self-Evolution 默认都不能直接覆盖正式文件。

默认流程：

```text
发现问题 → 生成 proposal → 写入 review_queue → 用户审批 → 应用变更
```

只有配置显式开启 `AUTO_APPLY_*` 时，才允许自动应用，并且仍要先 snapshot。

### 3.4 自进化不是模型变聪明

系统文档和 README 必须明确写出：

```text
所谓自进化，是 Skill / Memory / Prompt 注入质量提升，不是模型权重变化，不是模型推理能力提升。
```

### 3.5 评测优先于自评

不能只让 LLM 自己说“我优化得更好了”。

所有 Prompt / Skill 优化都必须经过：

```text
固定评测样本
明确 metric
baseline score
candidate score
diff
人工确认
```

---

## 4. 推荐最终目录结构

```text
general-agent-framework/
  README.md
  pyproject.toml
  .env.example
  .gitignore

  app/
    __init__.py
    main.py
    cli.py

    core/
      config.py
      logging.py
      agent_factory.py
      runtime.py
      schemas.py
      model_router.py               # 主模型 / 辅助模型 / 反思模型路由
      permissions.py

    task/
      models.py
      store.py
      service.py
      runner.py
      evaluator.py                  # 任务执行后评估

    memory/
      hot_memory.py
      cold_memory.py
      fts.py
      summarizer.py
      tools.py
      curator.py                    # Memory Curator
      pii_filter.py                 # 敏感信息过滤

    skills/
      loader.py
      manager.py
      metadata.py                   # SkillMeta 数据结构
      usage.py                      # 使用统计
      provenance.py                 # 血统隔离
      curator.py                    # Skill Curator 主逻辑
      curator_prompts.py
      snapshot.py                   # Skill 快照
      archive.py                    # 归档与恢复
      diff.py                       # Skill diff
      tools.py

    nudge/
      reviewer.py
      prompts.py
      queue.py

    evolution/
      datasets.py                   # 评测集管理
      metrics.py                    # metric 注册
      prompt_registry.py            # prompt/skill 版本注册
      optimizer_base.py
      mipro_optimizer.py            # 可选
      gepa_optimizer.py             # 可选
      runner.py
      reports.py

    tools/
      registry.py
      file_tools.py
      web_tools.py
      task_tools.py
      shell_tools.py

    review/
      queue.py
      apply.py
      reject.py
      schemas.py

    api/
      routes_health.py
      routes_tasks.py
      routes_chat.py
      routes_memory.py
      routes_skills.py
      routes_reviews.py
      routes_curator.py
      routes_evolution.py

    web/
      index.html
      app.js
      style.css

  runtime/
    workspace/
    memory/
      MEMORY.md
      USER.md
    skills/
      report-writer/
        SKILL.md
      .archive/
      .snapshots/
    db/
      app.sqlite3
    review_queue/
    curator_reports/
    evolution_runs/
    evalsets/
```

---

## 5. 配置项

`.env.example` 需要新增：

```env
# ===== 基础 =====
APP_NAME=general-agent-framework
APP_ENV=dev

LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_API_KEY=
LLM_BASE_URL=

# ===== 辅助模型路由 =====
AUX_LLM_PROVIDER=
AUX_LLM_MODEL=
AUX_LLM_API_KEY=
AUX_LLM_BASE_URL=

REFLECTION_LLM_PROVIDER=
REFLECTION_LLM_MODEL=
REFLECTION_LLM_API_KEY=
REFLECTION_LLM_BASE_URL=

# ===== 路径 =====
RUNTIME_DIR=./runtime
WORKSPACE_DIR=./runtime/workspace
SQLITE_PATH=./runtime/db/app.sqlite3

# ===== Nudge =====
ENABLE_NUDGE=true
NUDGE_INTERVAL_TASKS=10
NUDGE_AUTO_APPLY=false

# ===== Curator =====
ENABLE_CURATOR=true
CURATOR_INTERVAL_HOURS=168
CURATOR_MIN_IDLE_HOURS=2
CURATOR_STALE_AFTER_DAYS=30
CURATOR_ARCHIVE_AFTER_DAYS=90
CURATOR_AUTO_APPLY=false
CURATOR_DRY_RUN_DEFAULT=true

# ===== Self Evolution =====
ENABLE_EVOLUTION=false
EVOLUTION_ENGINE=none       # none | mipro | gepa
EVOLUTION_AUTO_APPLY=false
EVOLUTION_MIN_SCORE_DELTA=0.05
EVOLUTION_MAX_COST_USD=2.00

# ===== Safety =====
HITL_REQUIRED_FOR_WRITE=true
HITL_REQUIRED_FOR_SKILL_CHANGE=true
HITL_REQUIRED_FOR_MEMORY_CHANGE=true
ENABLE_SAFE_SHELL=false
ENABLE_WEB_TOOLS=false
```

---

## 6. 数据库升级

在 SQLite 中新增或扩展以下表。

### 6.1 skills

```sql
CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  path TEXT NOT NULL,
  description TEXT,
  created_by TEXT NOT NULL DEFAULT 'user',
  source TEXT NOT NULL DEFAULT 'local',
  state TEXT NOT NULL DEFAULT 'active',
  pinned INTEGER NOT NULL DEFAULT 0,
  bundled INTEGER NOT NULL DEFAULT 0,
  hub_installed INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT,
  archived_at TEXT
);
```

字段要求：

```text
created_by: user | agent | system | hub
source: local | nudge | curator | import | hub
state: active | stale | archived
pinned: 用户强保护
bundled: 系统内置
hub_installed: 外部安装
```

### 6.2 skill_usage_events

```sql
CREATE TABLE IF NOT EXISTS skill_usage_events (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL,
  task_id TEXT,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT
);
```

事件类型：

```text
loaded
selected
read
applied
updated
archived
restored
pinned
unpinned
```

### 6.3 curator_runs

```sql
CREATE TABLE IF NOT EXISTS curator_runs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  phase1_counts_json TEXT,
  phase2_summary_json TEXT,
  snapshot_id TEXT,
  report_path TEXT,
  error_message TEXT
);
```

### 6.4 skill_snapshots

```sql
CREATE TABLE IF NOT EXISTS skill_snapshots (
  id TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  snapshot_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT
);
```

### 6.5 evolution_runs

```sql
CREATE TABLE IF NOT EXISTS evolution_runs (
  id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_name TEXT NOT NULL,
  engine TEXT NOT NULL,
  status TEXT NOT NULL,
  baseline_score REAL,
  candidate_score REAL,
  score_delta REAL,
  evalset_id TEXT,
  report_path TEXT,
  proposal_path TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error_message TEXT
);
```

---

## 7. Milestone 任务清单

# M0：升级项目骨架

## 任务

- [ ] 基于上一版 `智能体lite` 增加 `skills/curator.py`、`skills/provenance.py`、`skills/snapshot.py`、`evolution/`、`review/` 等模块。
- [ ] 更新 `README.md` 中的项目定位。
- [ ] 更新 `.env.example`。
- [ ] 增加数据库 migration 初始化逻辑。
- [ ] 所有新增功能默认安全关闭或 dry-run。

## 验收

运行：

```bash
python -m app.cli config
python -m app.cli db init
uvicorn app.main:app --reload --port 8080
```

均能正常执行。

---

# M1：Skill 元数据与使用统计

## 任务

实现 `app/skills/metadata.py` 和 `app/skills/usage.py`。

功能：

- [ ] 扫描 `runtime/skills/**/SKILL.md`。
- [ ] 解析 YAML frontmatter。
- [ ] 计算 `content_hash`。
- [ ] 将 Skill 注册到 `skills` 表。
- [ ] 支持字段：
  - `created_by`
  - `source`
  - `state`
  - `pinned`
  - `bundled`
  - `hub_installed`
  - `last_used_at`
  - `version`
- [ ] 每次 Agent 读取或使用 Skill 时记录 `skill_usage_events`。
- [ ] 提供 CLI：
  - `skills list`
  - `skills show <name>`
  - `skills usage <name>`

## 验收

```bash
python -m app.cli skills scan
python -m app.cli skills list
python -m app.cli skills show report-writer
```

能看到完整 metadata。

---

# M2：Skill 血统隔离 Provenance

## 任务

实现 `app/skills/provenance.py`。

核心设计：

```python
from contextvars import ContextVar

_write_origin = ContextVar("skill_write_origin", default="foreground")
BACKGROUND_REVIEW = "background_review"
FOREGROUND = "foreground"
```

实现 API：

- [ ] `set_current_write_origin(origin: str)`
- [ ] `reset_current_write_origin(token)`
- [ ] `get_current_write_origin()`
- [ ] `is_background_review()`
- [ ] `with_write_origin(origin)` context manager

Skill 写入规则：

```text
foreground 用户写入      → created_by=user
background_review 写入   → created_by=agent
system 初始化写入        → created_by=system
hub 安装写入             → created_by=hub
```

必须覆盖：

- Nudge review agent；
- Curator review agent；
- Evolution review agent；
- 用户通过 CLI/API/Web 创建 Skill。

## 验收

测试：

```bash
pytest tests/test_skill_provenance.py -q
```

必须验证：

- 用户前台创建的 Skill 不标记为 agent；
- 后台 review 创建的 Skill 标记为 agent；
- Curator 候选只包含 `created_by=agent`；
- `pinned=true` 的 Skill 永远不进入 Curator 候选。

---

# M3：Skill 快照、归档和恢复

## 任务

实现 `app/skills/snapshot.py` 和 `app/skills/archive.py`。

功能：

- [ ] `snapshot_skills(reason: str)`：将整个 `runtime/skills/` 复制到 `runtime/skills/.snapshots/<timestamp>-<reason>/`。
- [ ] snapshot 记录写入 `skill_snapshots` 表。
- [ ] `archive_skill(name)`：移动到 `runtime/skills/.archive/<name>/`。
- [ ] `restore_skill(name)`：从 `.archive` 恢复到主目录。
- [ ] archive 不允许覆盖现有同名 active skill。
- [ ] archive 保留完整目录，包括 `references/`、`templates/`、`scripts/`、`assets/`。
- [ ] 提供 CLI：
  - `skills snapshot --reason pre-curator-run`
  - `skills archive <name>`
  - `skills restore <name>`
  - `skills snapshots`

## 验收

```bash
python -m app.cli skills snapshot --reason test
python -m app.cli skills archive old-skill
python -m app.cli skills restore old-skill
```

文件完整保留，数据库状态同步正确。

---

# M4：Curator 触发机制

## 任务

实现 `app/skills/curator.py` 的触发层。

不是 cron，不启动独立守护进程。采用 idle-triggered / tick-triggered：

触发入口：

```text
任务完成后
应用启动后
Web/API tick
手动 CLI curator run
```

实现函数：

- [ ] `load_curator_state()`
- [ ] `save_curator_state()`
- [ ] `is_curator_enabled()`
- [ ] `is_curator_paused()`
- [ ] `should_run_now(now=None)`
- [ ] `maybe_run_curator(idle_for_seconds=None)`
- [ ] `pause_curator()`
- [ ] `resume_curator()`
- [ ] `curator_status()`

默认门禁：

```text
enabled=true
paused=false
first run 只 seed last_run_at，不立刻跑
距离上次运行 >= interval_hours
如果提供 idle_for_seconds，则 idle >= min_idle_hours
任何异常都不能阻断主任务流程
```

CLI：

```bash
python -m app.cli curator status
python -m app.cli curator pause
python -m app.cli curator resume
python -m app.cli curator run --dry-run
```

## 验收

测试覆盖：

- disabled 不跑；
- paused 不跑；
- first-run 只 seed；
- interval 未到不跑；
- interval 到达放行；
- idle 不够不跑；
- 异常只记录日志，不影响主任务。

---

# M5：Curator Phase 1 自动状态迁移

## 任务

实现 `apply_automatic_transitions()`。

规则：

```text
只处理 created_by=agent 的 Skill
pinned=true 跳过
bundled=true 跳过
hub_installed=true 跳过

active 超过 stale_after_days 未使用 → stale
stale 超过 archive_after_days 未使用 → archived
stale 在 stale_after_days 内重新使用 → active
archived 不自动恢复
```

注意：

- 这一步不调用 LLM；
- 这是纯规则状态机；
- 即使 Phase 2 失败，Phase 1 也可以独立成功；
- dry-run 模式只生成计划，不实际修改。

输出：

```json
{
  "checked": 12,
  "marked_stale": 3,
  "archived": 1,
  "reactivated": 2,
  "skipped_pinned": 1,
  "skipped_user": 5
}
```

## 验收

```bash
python -m app.cli curator run-phase1 --dry-run
```

显示将被 stale / archive / reactivate 的 Skill 列表。

---

# M6：Curator Phase 2 LLM Review

## 任务

实现 LLM review agent，但默认只生成 proposal，不自动应用。

Phase 2 输入：

- Phase 1 结果；
- agent-created active/stale Skill 列表；
- 每个 Skill 的 description、body 摘要、last_used_at、content_hash；
- usage events；
- 当前 snapshot_id。

Phase 2 目标：

```text
不是被动审计，也不是简单查重，而是 umbrella-building consolidation。
```

要求 review agent 生成：

- [ ] 候选 cluster；
- [ ] 建议合并为 umbrella skill；
- [ ] 建议 archive 的窄 Skill；
- [ ] 建议拆分或重写的 Skill；
- [ ] 不处理原因；
- [ ] 风险说明；
- [ ] 应用计划。

Hard rules：

```text
1. 不动 created_by=user 的 Skill
2. 不动 bundled Skill
3. 不动 hub_installed Skill
4. 不动 pinned Skill
5. 不删除，只 archive
6. 不因为 use_count=0 就认为无价值
7. 不因为 trigger 不同就拒绝 umbrella 合并
8. 输出必须是结构化 JSON + Markdown report
```

运行约束：

```text
使用辅助模型 AUX_LLM
skip_memory=true
skip_context_files=true
quiet_mode=true
禁止递归触发 curator
限制 token budget
默认 dry-run
```

输出文件：

```text
runtime/curator_reports/<run_id>.md
runtime/review_queue/<run_id>-curator-proposal.md
```

## 验收

```bash
python -m app.cli curator run --dry-run
python -m app.cli review list
```

能看到 curator proposal，但正式 Skill 不被修改。

---

# M7：Curator 应用、Diff 与审批

## 任务

实现 `app/review/apply.py` 和 `app/skills/diff.py`。

功能：

- [ ] 对 Curator proposal 生成文件级 diff。
- [ ] 用户可以 approve / reject。
- [ ] apply 前必须 snapshot。
- [ ] apply 后更新 skills 表状态。
- [ ] apply 后写 curator_runs summary。
- [ ] 支持 rollback 到 snapshot。

CLI：

```bash
python -m app.cli review show <review_id>
python -m app.cli review diff <review_id>
python -m app.cli review apply <review_id>
python -m app.cli review reject <review_id>
python -m app.cli curator rollback <snapshot_id>
```

API：

```http
GET  /reviews/{id}
GET  /reviews/{id}/diff
POST /reviews/{id}/apply
POST /reviews/{id}/reject
POST /curator/rollback/{snapshot_id}
```

## 验收

Curator proposal 不会自动改正式文件；用户 apply 后才生效；可以 rollback。

---

# M8：Memory Curator

## 任务

实现 `app/memory/curator.py`。

目标：

```text
治理 MEMORY.md / USER.md / 冷记忆摘要的膨胀和重复。
```

功能：

- [ ] 检测 MEMORY.md 中重复规则；
- [ ] 检测过期偏好；
- [ ] 检测和 USER.md 冲突的信息；
- [ ] 检测敏感信息，例如 API Key、密码、Token；
- [ ] 生成 memory update proposal；
- [ ] 默认不直接改 MEMORY.md / USER.md；
- [ ] 写入 review_queue。

Hard rules：

```text
1. 不自动写入敏感信息
2. 不根据单次任务写长期记忆
3. 不覆盖用户显式写入的偏好
4. 只生成提议，用户审批后应用
```

CLI：

```bash
python -m app.cli memory curate --dry-run
python -m app.cli memory proposals
```

## 验收

人为在 `MEMORY.md` 写入重复规则和伪 API Key，运行 curator 后能生成清理提议，但不自动修改文件。

---

# M9：Prompt / Skill 评测集管理

## 任务

实现 `app/evolution/datasets.py`。

评测集目录：

```text
runtime/evalsets/
  report-writer/
    evalset.jsonl
```

样本格式：

```json
{"input": "生成一份技术调研报告", "expected_contains": ["背景", "目标", "结论"], "tags": ["report"]}
```

支持样本类型：

- [ ] `expected_contains`
- [ ] `expected_regex`
- [ ] `json_valid`
- [ ] `markdown_sections`
- [ ] `llm_judge_pairwise`，可选
- [ ] `artifact_exists`
- [ ] `custom_python_metric`，默认关闭

CLI：

```bash
python -m app.cli evalset list
python -m app.cli evalset show report-writer
python -m app.cli evalset add report-writer sample.json
```

## 验收

能为默认 `report-writer` Skill 创建 5 条评测样本。

---

# M10：Metric 注册与任务评估

## 任务

实现 `app/evolution/metrics.py` 和 `app/task/evaluator.py`。

内置 metric：

| metric | 说明 |
|---|---|
| `contains_required_sections` | 检查 Markdown 标题 |
| `json_valid` | 检查 JSON 合法性 |
| `artifact_exists` | 检查产物文件 |
| `regex_match` | 正则匹配 |
| `length_range` | 输出长度范围 |
| `llm_pairwise_judge` | 可选，需辅助模型 |

要求：

- [ ] 每次任务完成后可选评估；
- [ ] 评估结果写入数据库；
- [ ] evolution 只能基于 metric 结果，不允许只用 LLM 自评；
- [ ] LLM judge 必须标记为弱证据，不能作为唯一应用依据。

## 验收

```bash
python -m app.cli task eval <task_id>
```

能输出分数、失败原因和涉及 metric。

---

# M11：Prompt / Skill 版本注册

## 任务

实现 `app/evolution/prompt_registry.py`。

目标：

```text
任何被优化的 prompt / skill 都必须有版本、diff、score、可回滚。
```

功能：

- [ ] 保存 baseline 版本；
- [ ] 保存 candidate 版本；
- [ ] 保存 score；
- [ ] 保存 evalset；
- [ ] 保存 optimizer engine；
- [ ] 保存 diff；
- [ ] 支持 promote / rollback。

CLI：

```bash
python -m app.cli prompts list
python -m app.cli prompts show <name>
python -m app.cli prompts diff <name> --from v1 --to v2
python -m app.cli prompts promote <name> --version v2
python -m app.cli prompts rollback <name> --version v1
```

## 验收

优化后的 Skill 不会直接覆盖原文件，而是进入候选版本；用户 promote 后才生效。

---

# M12：MIPROv2 / GEPA 优化管线，可选增强

## 任务

实现 `app/evolution/runner.py`、`mipro_optimizer.py`、`gepa_optimizer.py`。

要求：

- [ ] 如果安装 `dspy-ai`，支持 `MIPROv2` 和 `GEPA`。
- [ ] 如果未安装，系统仍能运行，只提示可选依赖缺失。
- [ ] 默认 `ENABLE_EVOLUTION=false`。
- [ ] 默认 `EVOLUTION_ENGINE=none`。
- [ ] 支持只对指定 Skill 运行优化。
- [ ] 支持 max cost 限制。
- [ ] 支持 max iterations 限制。
- [ ] 支持 trainset / valset 分离。
- [ ] 输出 baseline score、candidate score、score_delta。
- [ ] score_delta 未达到阈值，不允许 promote。
- [ ] 优化结果写入 review_queue，不自动应用。

推荐渐进路径：

```text
先实现 MIPROv2 stub / 简化版
再接 dspy.MIPROv2
最后接 dspy.GEPA
```

GEPA 的边界必须写入 README：

```text
GEPA 优化的是 prompt / instruction / skill 文本，不改变模型权重。
```

CLI：

```bash
python -m app.cli evolution run --target-skill report-writer --engine gepa --dry-run
python -m app.cli evolution show <run_id>
python -m app.cli evolution diff <run_id>
python -m app.cli evolution promote <run_id>
```

## 验收

用 `report-writer` 的 evalset 跑一次 evolution dry-run，生成候选 Skill 文本、score 对比和 review proposal。

---

# M13：Nudge 升级为“生产端”，Curator 升级为“质检端”

## 任务

重构 `app/nudge/`。

Nudge 只负责发现可能值得沉淀的经验：

```text
任务完成 → 识别可沉淀经验 → 生成新 Skill / Memory 提议
```

Curator 负责整理 Nudge 产生的产物：

```text
Nudge 产物变多 → Curator 检查重复、狭窄、过期、可合并项
```

实现：

- [ ] Nudge 生成的 Skill 默认 `created_by=agent`、`source=nudge`。
- [ ] 用户手动创建的 Skill 默认 `created_by=user`、`source=local`。
- [ ] Curator 只处理 `created_by=agent`。
- [ ] 每个 Nudge proposal 必须关联 source_task_id。
- [ ] 每个 Curator proposal 必须关联 candidate skills。

## 验收

创建一个用户 Skill 和一个 Nudge Skill，运行 Curator，只有 Nudge Skill 进入候选列表。

---

# M14：Skill Health Dashboard

## 任务

升级极简 Web 前端，增加 Skill 健康页。

页面功能：

- [ ] Skill 列表；
- [ ] 按 state 过滤 active / stale / archived；
- [ ] 按 created_by 过滤 user / agent / system / hub；
- [ ] 显示 last_used_at；
- [ ] 显示 use_count；
- [ ] 显示 pinned 状态；
- [ ] 支持 pin / unpin；
- [ ] 支持查看 Skill 内容；
- [ ] 支持查看 Curator proposal diff；
- [ ] 支持 restore archived Skill。

页面入口：

```text
/
  任务台
/skills
  Skill 管理
/curator
  Curator 状态
/reviews
  审批队列
/evolution
  自进化运行记录
```

不要求复杂前端框架，继续使用原生 HTML/CSS/JS 即可。

## 验收

浏览器访问：

```text
http://127.0.0.1:8080/skills
```

可以查看、pin、恢复 Skill。

---

# M15：Curator 状态页与报告页

## 任务

Web 增加 Curator 页面：

显示：

- [ ] enabled / paused；
- [ ] last_run_at；
- [ ] next_eligible_at；
- [ ] interval_hours；
- [ ] stale_after_days；
- [ ] archive_after_days；
- [ ] 最近 10 次 curator_runs；
- [ ] 每次 run 的 phase1 counts；
- [ ] 每次 run 的 report 链接；
- [ ] 手动 run dry-run 按钮；
- [ ] pause / resume 按钮。

API：

```http
GET  /curator/status
POST /curator/run
POST /curator/pause
POST /curator/resume
GET  /curator/runs
GET  /curator/runs/{run_id}
```

## 验收

Web 页面可手动触发 dry-run，并展示报告。

---

# M16：审计日志与可观测性

## 任务

所有关键动作必须写事件日志：

事件类型：

```text
task.created
task.started
task.completed
task.failed

skill.created
skill.updated
skill.archived
skill.restored
skill.pinned
skill.unpinned

curator.seeded
curator.skipped
curator.started
curator.phase1_completed
curator.phase2_completed
curator.proposal_created
curator.applied
curator.failed

evolution.started
evolution.completed
evolution.proposal_created
evolution.promoted
evolution.failed
```

实现：

- [ ] SQLite event log；
- [ ] CLI 查看；
- [ ] Web 查看最近事件；
- [ ] 关键 report 文件链接。

CLI：

```bash
python -m app.cli events tail
python -m app.cli events search curator
```

## 验收

执行任务、Nudge、Curator、Evolution 后能查到事件链路。

---

# M17：安全测试与回归测试

## 任务

新增测试：

```text
tests/
  test_skill_metadata.py
  test_skill_usage.py
  test_skill_provenance.py
  test_skill_snapshot_archive.py
  test_curator_trigger.py
  test_curator_phase1.py
  test_curator_phase2_proposal.py
  test_memory_curator.py
  test_evalset_metrics.py
  test_evolution_registry.py
  test_review_apply_rollback.py
```

必须覆盖：

- [ ] 用户 Skill 不进入 Curator；
- [ ] pinned Skill 不进入 Curator；
- [ ] system / hub Skill 不进入 Curator；
- [ ] agent Skill 可进入 Curator；
- [ ] archive 不删除文件；
- [ ] restore 可恢复；
- [ ] snapshot 可创建；
- [ ] first-run curator 只 seed 不运行；
- [ ] dry-run 不修改正式文件；
- [ ] proposal 未审批不应用；
- [ ] evolution score 不达标不能 promote；
- [ ] MEMORY.md 敏感信息不自动写入；
- [ ] shell 默认关闭。

## 验收

```bash
pytest -q
```

测试通过。

---

# M18：README 与交付文档

## README 必须新增章节

1. 项目定位；
2. 和 Hermes-Lite 的区别；
3. 什么是 Curator；
4. 什么是 Provenance 血统隔离；
5. 为什么 Curator 不直接删除；
6. Skill 生命周期；
7. Nudge 和 Curator 的分工；
8. 自进化真实边界；
9. GEPA / MIPROv2 可选优化；
10. 为什么评测优先于自评；
11. 如何运行 dry-run；
12. 如何审批 proposal；
13. 如何 rollback；
14. Web 页面说明；
15. 安全默认值；
16. 常见问题。

## 必须明确写出的边界

```text
本项目不会训练模型。
本项目不会修改模型权重。
本项目不会保证越用越聪明。
本项目提升的是任务上下文、Skill、Memory、Prompt 的组织质量。
所有后台自我改进默认只生成提议，用户批准后才应用。
```

---

## 8. 推荐实现顺序

```text
P0  数据库升级 + Skill metadata
P1  Provenance 血统隔离
P2  Snapshot / Archive / Restore
P3  Curator 触发机制
P4  Curator Phase 1 状态机
P5  Curator Phase 2 proposal
P6  Review diff / apply / rollback
P7  Memory Curator
P8  Evalset + Metrics
P9  Prompt / Skill version registry
P10 MIPROv2 / GEPA 可选优化
P11 Web Skill / Curator / Review 页面
P12 审计日志
P13 测试与 README
```

不要一开始做 GEPA。先把 Curator 的治理闭环做稳，再加优化器。

---

## 9. 最小可运行验收链路

Trae 完成后，至少跑通下面链路。

### 9.1 初始化

```bash
python -m app.cli db init
python -m app.cli skills scan
python -m app.cli skills list
```

### 9.2 创建两类 Skill

```bash
python -m app.cli skills create user-note --created-by user
python -m app.cli nudge simulate-create-skill agent-note
```

验收：

```text
user-note created_by=user
agent-note created_by=agent
```

### 9.3 Curator dry-run

```bash
python -m app.cli curator run --dry-run
```

验收：

```text
候选只包含 agent-note
user-note 不进入候选
没有正式文件被修改
生成 curator report
生成 review proposal
```

### 9.4 审批并应用

```bash
python -m app.cli review list
python -m app.cli review diff <review_id>
python -m app.cli review apply <review_id>
```

验收：

```text
apply 前创建 snapshot
apply 后 Skill 状态更新
事件日志完整
可以 rollback
```

### 9.5 Archive / Restore

```bash
python -m app.cli skills archive agent-note
python -m app.cli skills restore agent-note
```

验收：

```text
archive 不删除内容
restore 后内容完整
```

### 9.6 Evolution dry-run

```bash
python -m app.cli evalset add report-writer examples/report_evalset.jsonl
python -m app.cli evolution run --target-skill report-writer --engine mipro --dry-run
```

验收：

```text
生成 baseline score
生成 candidate score
生成 diff
写入 review_queue
不自动覆盖 report-writer
```

---

## 10. 最终完成标准

项目完成后必须具备：

- [ ] 自主任务执行；
- [ ] Skill 加载；
- [ ] Skill metadata；
- [ ] Skill usage tracking；
- [ ] created_by 血统隔离；
- [ ] pinned / bundled / hub-installed 保护；
- [ ] Curator status；
- [ ] Curator dry-run；
- [ ] Curator Phase 1 状态机；
- [ ] Curator Phase 2 proposal；
- [ ] snapshot；
- [ ] archive；
- [ ] restore；
- [ ] review diff；
- [ ] apply / reject；
- [ ] rollback；
- [ ] Memory Curator；
- [ ] evalset；
- [ ] metrics；
- [ ] prompt / skill version registry；
- [ ] evolution dry-run；
- [ ] Web Skill 管理页；
- [ ] Web Curator 状态页；
- [ ] Web Review 审批页；
- [ ] 审计日志；
- [ ] 完整测试；
- [ ] 完整 README。

---

## 11. 最终系统能力描述

完成后，这个项目应该能够被描述为：

```text
一个基于智能体的通用智能体框架。
它不仅能接收任务、调用工具、生成产物，还能沉淀经验、管理 Skill 生命周期、保护用户资产、定期整理 Agent 生成的 Skill、生成可审批的自我改进提议，并通过评测集验证 Prompt / Skill 优化效果。
```

它仍然不是：

```text
模型训练平台
自动微调系统
Claude Code 替代品
完全自动无人监管系统
```

它是：

```text
一个具备长期使用治理能力的 Agent Harness 产品原型。
```
