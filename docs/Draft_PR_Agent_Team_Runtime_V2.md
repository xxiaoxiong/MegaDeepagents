# Draft PR: TASK_TEAM Agent Team Runtime v2

## Summary

This refactor upgrades the existing TASK_TEAM path from a persistent DAG dispatcher into a governed,
long-lived coding-agent runtime while preserving `TeamRuntimeFacade` as the production entry point.

## Architecture changes

- Adds durable `TeammateSession`, actor/supervisor, command/event queues and stable restart identity.
- Adds 19 governed team tools backed by one audited Control Plane.
- Makes SQLite TaskBoard the runtime authority and versioned TaskGraph mutations the plan authority.
- Carries only verified, same-run, hash-valid Artifact evidence to direct dependants.
- Adds per-Agent Git worktrees, leases, explicit gitignored environment-file allowlists, commits,
  merge queue, conflict detection and integration gates.
- Makes production verification fail-closed and compiles OutputContract into executable checks.
- Adds structured permission requests, plan approval, lifecycle hooks, dynamic teams and Lead recommendations.
- Adds tool-level cancellation, atomic writes, structured shell policies and side-effect idempotency logs.
- Adds replayable event envelopes and TASK_TEAM APIs for sessions, graph, permissions, plans, Git,
  verification, errors and Artifact lineage.
- Reframes `DISCUSSION/TeamRunner` as Legacy; new functionality stays on TASK_TEAM.

## Database migration

Schema version advances from 3 to 4. Migration is additive and lazy via `CREATE TABLE IF NOT EXISTS`.
Existing run, Board, Agent, Artifact and Mailbox rows remain in place. See
`docs/Agent_Team_Parity_Audit.md` for the complete table list and rollback guidance.

## Test coverage

- Existing deterministic/offline suite
- New `tests/test_agent_team_runtime_v2.py`
- Temporary real Git repositories for worktree/integration/conflict tests
- Optional live model tests remain opt-in with `RUN_LIVE_MODEL_TESTS=1`

Final command/result (to be updated immediately before publishing):

```text
pytest -m "not live_model and not real_langsmith" --ignore=tests/test_observability.py
494 passed, 5 deselected
```

`tests/test_observability.py` was run separately in offline/mock mode: 20 tests passed, then the only
configuration expectation was corrected. The execution sandbox refused the final re-run because that
file deliberately toggles LangSmith enabled state, even though `RunTree.post/patch` are mocked. No trace
export was attempted.

## Known limitations

- Real model and LangSmith tests require external credentials.
- Remote push/Draft PR publication requires GitHub authentication; no remote success is fabricated.
- SQLite provides single-host durability; multi-host deployment needs a shared transactional store/queue.
- Legacy DISCUSSION code remains for compatibility but is frozen for new features.

## Review guide

1. Start with `docs/Agent_Team_Parity_Audit.md` and `team_runtime.py`.
2. Review state authority in `transactional_task_service.py`, `task_board.py` and
   `parallel_scheduler.py`.
3. Review security boundaries in `permission.py`, `shell_policy.py`, `executor.py` and
   `git_workspace.py`.
4. Review recovery in `teammate_session.py`, `resume_coordinator.py`, `artifact.py` and
   `phase_g_store.py`.
5. Run the default non-live suite, then opt into live tests only with dedicated credentials.
