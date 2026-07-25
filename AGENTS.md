# MegaDeepagents contributor guide

## Canonical V3 runtime

All new browser, API, and CLI runs must enter:

```text
RunApplicationService
  → TeamRuntimeFacade
  → app.runtime.root_graph
  → TaskGraph / TransactionalTaskService / TaskBoard
  → ParallelTeamScheduler
  → DeepAgentExecutor
  → ArtifactStore / Verifier
```

Do not add another scheduler, orchestrator, task status enum, SQLite file, SSE
protocol, Artifact scanner, or production fake executor.

## Ownership rules

- LangGraph owns orchestration position, branching, interrupt, resume and
  checkpoint state.
- `TaskGraph` owns the versioned plan, dependencies and output contracts.
- `TaskBoard` owns task status, claims, attempts and cancellation gates.
- Supervisor emits `SupervisorDecision`; it never writes domain state directly.
- Worker Agents produce artifacts and evidence only.
- Verifier is the only success gate and must fail closed.
- ArtifactStore owns files, SHA-256, versions and lineage.
- Event Envelope in SQLite is the replay source for SSE.
- Git worktrees, permissions and plan approval are governed control-plane
  operations.

## Compatibility

`DISCUSSION/TeamRunner` is frozen compatibility code. Never accept a new
DISCUSSION run or route V3 work into the round-chat runtime. Historical records
may remain readable and cancellable for one migration window. Old
`/team-tasks` endpoints are adapters over TASK_TEAM; public clients use
`/api/v1`.

## Database

Use `app.infrastructure.database.connection` and the configured
`DATABASE_URL`/`SQLITE_PATH`. Do not open an independent application database.
Use transactions for multi-write mutations. Task identifiers are scoped by
`run_id`; never query a potentially ambiguous `task_id` without its Run.

Do not change the LangGraph checkpointer connection into the application
connection. It is intentionally separate to protect saver transactions.

## API and frontend

- Public routes live in `app/api/v1`; use Pydantic request/response models.
- Validate the Run boundary before returning Agent, Task or Artifact data.
- Never expose API keys or absolute host filesystem paths.
- Artifact reads must resolve inside the Run workspace and reject path escape.
- New real-time UI state must be persisted before it is streamed.
- Vue code lives in `frontend/` and consumes real `/api/v1` data.
- Keep Vercel frontend-only. The durable backend runs in the Docker image.

## Verification

Run before delivery:

```bash
python -m compileall -q app
pytest -m "not live_model and not real_langsmith"
npm --prefix frontend test
npm --prefix frontend run build
```

Never delete a valid test, weaken an assertion, treat missing model output as
success, or add a skip to manufacture green results. Test doubles may only
enter through explicit test injection seams.

## Documentation

Keep these files aligned with implementation:

- `README.md`
- `docs/architecture.md`
- `docs/api.md`
- `docs/database.md`
- `docs/development.md`
- `docs/deployment.md`
- `docs/frontend.md`
- `docs/testing.md`
- `docs/migration-v3.md`
- `docs/refactor-v3/*`
