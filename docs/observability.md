# 可观测性集成文档

## 概述

本项目的 LangSmith 可观测性集成将 LLM 调用、多 Agent 团队协作循环、单 Agent 任务执行等关键阶段的结构化 trace 上报到 [LangSmith](https://smith.langchain.com/) 平台，支持事后回放、Token 用量分析、成本跟踪和质量评测。

**设计原则**：

- **默认关闭**——未配置 `.env` 时框架本地可跑，零外网依赖（`docs/updatePlan.md` 硬性约束）
- **可选依赖**——`langsmith` 放在 `pyproject.toml [optional-dependencies]`，未安装时自动降级
- **离线摘要**——关闭上报时仍写本地 `[trace]` 日志，便于快速排障
- **可选的 agent_type**——集成关注可观测，不影响结果语义

---

## 快速启用

### 1. 配置 `.env`

```env
# ===== LangSmith 可观测性 =====
LANGSMITH_ENABLED=true
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # 你的 API Key
LANGSMITH_PROJECT=multiagent-frame
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_TRACING=true
LANGSMITH_SAMPLE_RATE=1.0
LANGSMITH_OFFLINE_LOG=true
```

### 2. 可选安装 langsmith

```bash
pip install langsmith>=0.10.0
```

（`langsmith` 通常会随 `langchain` 自动安装，无需手动添加）

### 3. 检查是否生效

```bash
python -c "
from app.core.observability import init_observability
ctx = init_observability()
print('enabled:', ctx.enabled, 'client:', ctx.client is not None)
"
```

预期输出 `enabled: True client: True`。

---

## Trace 树结构

一条完整的多 Agent 团队任务在 LangSmith UI 上呈现为：

```
team_run (root span) — @traceable on TeamRunner.run()
 ├── select_speaker              — trace_span
 ├── adapter.run / agent_llm_call — @traceable LLM run (含 retry + attempt)
 │    └── ChatDeepSeek.invoke     — LangChain 自动子 span (token 用量在此)
 ├── process_actions             — trace_span
 ├── termination_check           — trace_span
 └── review_repair               — @traceable on ReviewRepairLoop
```

单 Agent 任务：

```
single_agent_run — @traceable on AgentRunner.run()
 ├── agent.invoke (deepagents 内部)  — 自动挂为 child (环境变量 LANGSMITH_TRACING)
 └── ChatDeepSeek.invoke            — LangChain 自动 LLM run
```

记忆摘要：

```
memorize_summary — @traceable on summarize_results()
 └── model.ainvoke — 自动挂为 child
```

---

## 9 个埋点位置

| 编号 | 位置 | 文件 | 行号 | run_type |
|---|---|---|---|---|
| T1 | team_run（顶层 span） | `app/multiagent/team_runner.py` `run()` | 174 | chain |
| T2 | team_round（每轮） | `team_runner.py` `run()` 内部 | 205-334 | chain |
| T3 | select_speaker | `team_runner.py` `run()` 内部 | 210-217 | chain |
| T4 | agent_llm_call | `app/multiagent/runtime_adapter.py` `_traced_llm_call()` | 332 | **llm** |
| T5 | process_actions | `team_runner.py` `run()` 内部 | 291-299 | chain |
| T6 | termination_check | `team_runner.py` `run()` 内部 | 326-332 | chain |
| T7 | review_repair | `app/multiagent/review_repair.py` `process_review_result()` | 55 | chain |
| T8 | single_agent_run | `app/task/runner.py` `run()` | 185 | chain |
| T9 | memorize_summary | `app/memory/summarizer.py` `summarize_results()` | 25 | chain |

**热点 T4**：每个 Agent 每轮的真实 LLM 调用，是最高频路径。`@traceable` 装饰后自动接收 `langsmith_extra` 动态 metadata（agent_name/role/attempt）。

---

## 持久化 trace URL

每条团队任务的每轮记录都会将当前 LangSmith run URL 写入 `team_rounds.langsmith_run_url` 列。

```sql
-- team_rounds 表（已兼容旧库自动补列）
SELECT round_number, selected_speaker, langsmith_run_url FROM team_rounds;
```

API 端点 `GET /team-tasks/{task_id}/rounds` 返回的 `RoundResponse` 现在包含 `langsmith_run_url` 字段。前端可直接渲染"在 LangSmith 查看 trace"按钮。

---

## 配置项参考

| 配置字段 | 默认值 | 说明 |
|---|---|---|
| `langsmith_enabled` | `false` | 总开关（默认关闭，不触外网） |
| `langsmith_api_key` | 空 | API Key；留空时即便开关为 true 也降级 offline |
| `langsmith_project` | `multiagent-frame` | LangSmith 项目名（对应 UI 左上角项目切换） |
| `langsmith_endpoint` | `https://api.smith.langchain.com` | API 端点 |
| `langsmith_tracing` | `true` | 开关开启且有 Key 时是否真发样本 |
| `langsmith_service_name` | `multiagent-frame` | 用于区分 API / CLI / pytest 运行 |
| `langsmith_sample_rate` | `1.0` | [0,1] 热路径采样率（降本用） |
| `langsmith_offline_log` | `true` | 关闭时是否把 trace 摘要打到本地 `[trace]` 日志 |

---

## 离线模式（默认）

开启（默认）时：
```
[trace] enter name=agent_llm_call type=llm meta={"agent_name":"Planner","attempt":1}
[trace] exit  name=agent_llm_call type=llm
[trace] enter name=select_speaker type=chain meta={"candidates":["Planner","Coder",...]}
[trace] exit  name=select_speaker type=chain
[trace] event  name=speaker_selected payload={"agent":"Coder","round":2}
```

聚合在 `runtime/logs/agent.log` 中，与普通 INFO 日志混排。所有装饰器 / 上下文管理器在关闭时不会产生任何网络开销。

---

## 测试

```bash
# 单元 + 离线测试（不需 API Key，零外网）
pytest tests/test_observability.py -v
# 预期：18 passed

# 完整回归（不含真实 LLM 测试）
pytest tests/ -v -k "not real_llm and not real_langsmith"
# 预期：110+ passed

# 真值 LangSmith 测试（需配置 API Key）
LANGSMITH_API_KEY=lsv2_pt_xxx pytest tests/ -m real_langsmith --tb=short
```

---

## 架构图

```
┌──────────────────────────────────────────────────┐
│  TeamRunner.run()  ← @traceable T1              │
│  ┌───────────────────────────────────────┐       │
│  │ with trace_span("round", T2):        │       │
│  │  ├─ trace_span("select_speaker", T3) │       │
│  │  ├─ _traced_llm_call(T4) ← @traceable│       │
│  │  │   └─ ChatDeepSeek.invoke (自动)    │       │
│  │  ├─ trace_span("process_actions", T5) │       │
│  │  └─ trace_span("termination", T6)    │       │
│  └───────────────────────────────────────┘       │
│  EventEmitter.emit() → emit_trace_event()        │
│  get_current_run_url() → team_rounds 落库        │
└──────────────────────────────────────────────────┘
```

---

## FAQ

**Q：LangSmith 没用上为什么还有网络错误消息？**
A：`test_multiagent_team_graph.py` 或真实 LLM 测试用 fake key 初始化了 `langsmith.Client`，403/401 错误是 SDK 自动重试上报导致的。不影响业务逻辑。不使用真实 LLM 的单元测试无此错误。

**Q：能在离线模式下验证 trace 形状吗？**
A：可以。`offline_log=True` 时每个 `traceable` 装饰的函数调用都会在 `agent.log` 打印 `[trace]` 行，包含 enter/exit 名称与 metadata。

**Q：SSE 事件和 LangSmith trace 是两套无关数据吗？**
A：既有 SSE 事件分发到前端，又通过 `EventEmitter.emit()` 内部的 `emit_trace_event()` 旁路分发一份到当前 LangSmith span 的 `add_event()`。两套信号源共享第一次 emit 的数据，不会重复构建 payload。
