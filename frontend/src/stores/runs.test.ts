import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useRunsStore } from "@/stores/runs";
import type { AgentRun, EventEnvelope, Task } from "@/types";

const event = (sequence: number): EventEnvelope => ({
  event_id: `evt_${sequence}`,
  run_id: "run_1",
  event_type: "task_started",
  sequence,
  timestamp: "2026-07-24T10:00:00Z",
  payload: {},
});

const run = (runId = "run_1"): AgentRun => ({
  run_id: runId,
  goal: "Exercise the canonical V3 runtime",
  mode: "team",
  resolved_mode: "team",
  team_template: "software_dev_team",
  status: "running",
  review_required: true,
  metadata: {},
});

const task = (taskId: string, title: string): Task => ({
  task_id: taskId,
  run_id: "run_1",
  title,
  objective: title,
  status: "running",
  dependencies: [],
  attempts: 1,
  max_attempts: 3,
  produced_artifact_ids: [],
  metadata: {},
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function mockSnapshot(events: EventEnvelope[] = []) {
  vi.spyOn(api, "getRun").mockResolvedValue(run());
  vi.spyOn(api, "listTasks").mockResolvedValue([]);
  vi.spyOn(api, "listAgents").mockResolvedValue([]);
  vi.spyOn(api, "listArtifacts").mockResolvedValue([]);
  vi.spyOn(api, "listAllEvents").mockResolvedValue(events);
  vi.spyOn(api, "listPermissions").mockResolvedValue([]);
  vi.spyOn(api, "listPlans").mockResolvedValue([]);
  vi.spyOn(api, "taskGraph").mockResolvedValue({
    root_task_id: null,
    version: 0,
    nodes: {},
  });
  vi.spyOn(api, "errors").mockResolvedValue({});
  vi.spyOn(api, "git").mockResolvedValue({});
  vi.spyOn(api, "diagnostics").mockResolvedValue({
    run_id: "run_1",
    status: "running",
    health: "healthy",
    phase: "executing",
    checked_at: "2026-07-24T10:00:00Z",
    stalled_threshold_seconds: 60,
    event_count: events.length,
    last_sequence: events.at(-1)?.sequence ?? 0,
    active_assignments: [],
    task_counts: {},
    retryable_task_ids: [],
    delayed_retries: [],
    blockers: [],
    recommended_action: "",
  });
  vi.spyOn(api, "execution").mockResolvedValue({
    run_id: "run_1",
    generated_at: "2026-07-24T10:00:00Z",
    summary: {
      event_count: events.length,
      wall_time_ms: 0,
      active_time_ms: 0,
      parallelism: 0,
      utilization: 0,
      peak_concurrency: 0,
      tool_call_count: 0,
      retry_count: 0,
      handoff_count: 0,
      artifact_count: 0,
      completed_tasks: 0,
      total_tasks: 0,
      critical_path: [],
      critical_path_remaining: 0,
    },
    agents: [],
    tasks: [],
    attention: [],
  });
}

describe("run event reducer", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setActivePinia(createPinia());
  });

  it("deduplicates replayed SSE envelopes and preserves sequence order", () => {
    const store = useRunsStore();
    store.applyEvent(event(2));
    store.applyEvent(event(1));
    store.applyEvent(event(2));

    expect(store.current.events.map((item) => item.sequence)).toEqual([1, 2]);
    expect(store.lastSequence).toBe(2);
  });

  it("merges SSE events that arrive while a same-run history snapshot is loading", async () => {
    mockSnapshot([event(1)]);
    const store = useRunsStore();
    await store.loadRun("run_1");

    const history = deferred<EventEnvelope[]>();
    vi.mocked(api.listAllEvents).mockImplementationOnce(() => history.promise);
    const reload = store.loadRun("run_1");
    store.applyEvent(event(3));
    history.resolve([event(1), event(2)]);
    await reload;

    expect(store.current.events.map((item) => item.sequence)).toEqual([1, 2, 3]);
    expect(store.lastSequence).toBe(3);
  });

  it("prevents an older live refresh from overwriting a newer task projection", async () => {
    mockSnapshot();
    const store = useRunsStore();
    await store.loadRun("run_1");

    const olderTasks = deferred<Task[]>();
    vi.mocked(api.listTasks)
      .mockImplementationOnce(() => olderTasks.promise)
      .mockResolvedValueOnce([task("task_new", "new projection")]);

    const older = store.refreshLiveData("run_1", ["tasks"]);
    const newer = store.refreshLiveData("run_1", ["tasks"]);
    await newer;
    olderTasks.resolve([task("task_old", "stale projection")]);
    await older;

    expect(store.current.tasks.map((item) => item.task_id)).toEqual([
      "task_new",
    ]);
  });
});
