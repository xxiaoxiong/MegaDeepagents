# V3 目标架构

```text
Vue 3 / Browser
  → FastAPI /api/v1 + replayable SSE
  → RunApplicationService
  → GovernedRunGraph (LangGraph)
      intake → complexity_router
        ├─ single_plan
        └─ team_supervisor → build_team
      → dispatch → collect → verify
        ├─ pass → finalize
        ├─ repair → build_team
        ├─ replan → team_supervisor
        └─ human → interrupt/resume
  → ParallelTeamScheduler
  → DeepAgentExecutor workers
  → governed control plane
  → SQLite + workspace + optional LangSmith
```

## 权威边界

- LangGraph checkpoint：执行位置和轻量编排状态。
- TaskGraph：计划、依赖、OutputContract、预算、版本。
- TaskBoard：认领、尝试、Worker 所有权和运行状态。
- ArtifactStore：文件、SHA-256、版本与 lineage。
- Permission/Plan store：人工决定。
- Event envelope：审计和 SSE 重放。

## 框架职责

- LangChain：模型、tool、structured output、provider 适配。
- LangGraph：根图、路由、interrupt、resume、repair、replan、finalize。
- DeepAgents：受限 Worker loop，不写全局权威状态。
- LangSmith：可选 trace/evaluation；离线运行不受影响。
- MegaDeepagents：控制面、Artifact、Verifier、权限、Git 和平台 UI。

Domain 不依赖 FastAPI/模型 provider；API 不写裸 SQL；Graph Node 通过 service/store；Agent 写操作通过控制面。
