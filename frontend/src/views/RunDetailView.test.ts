import { createPinia } from "pinia";
import { createApp, h, nextTick, ref } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import RunDetailView from "@/views/RunDetailView.vue";
import type { AgentRun, EventEnvelope, RunDiagnostics, RunExecution } from "@/types";

function run(runId: string): AgentRun {
  return {
    run_id: runId,
    goal: `Goal ${runId}`,
    mode: "team",
    resolved_mode: "team",
    team_template: "software_dev_team",
    status: "running",
    review_required: true,
    metadata: {},
  };
}

function event(runId: string, sequence: number): EventEnvelope {
  return {
    event_id: `${runId}_event_${sequence}`,
    run_id: runId,
    event_type: "TaskStarted",
    sequence,
    timestamp: "2026-07-24T10:00:00Z",
    payload: {},
  };
}

function diagnostics(runId: string): RunDiagnostics {
  return {
    run_id: runId,
    status: "running",
    health: "healthy",
    phase: "executing",
    checked_at: "2026-07-24T10:00:00Z",
    stalled_threshold_seconds: 60,
    event_count: 0,
    last_sequence: 0,
    active_assignments: [],
    task_counts: {},
    retryable_task_ids: [],
    delayed_retries: [],
    blockers: [],
    recommended_action: "",
  };
}

function execution(runId: string): RunExecution {
  return {
    run_id: runId,
    generated_at: "2026-07-24T10:00:00Z",
    summary: {
      event_count: 0,
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
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function mockRunApis(
  listEvents: (runId: string) => Promise<EventEnvelope[]>,
) {
  vi.spyOn(api, "getRun").mockImplementation(async (runId) => run(runId));
  vi.spyOn(api, "listTasks").mockResolvedValue([]);
  vi.spyOn(api, "listAgents").mockResolvedValue([]);
  vi.spyOn(api, "listArtifacts").mockResolvedValue([]);
  vi.spyOn(api, "listAllEvents").mockImplementation(listEvents);
  vi.spyOn(api, "listPermissions").mockResolvedValue([]);
  vi.spyOn(api, "listPlans").mockResolvedValue([]);
  vi.spyOn(api, "taskGraph").mockResolvedValue({
    root_task_id: null,
    version: 0,
    nodes: {},
  });
  vi.spyOn(api, "errors").mockResolvedValue({});
  vi.spyOn(api, "git").mockResolvedValue({});
  vi.spyOn(api, "diagnostics").mockImplementation(async (runId) =>
    diagnostics(runId),
  );
  vi.spyOn(api, "execution").mockImplementation(async (runId) =>
    execution(runId),
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RunDetailView replay and stream lifecycle", () => {
  it("waits for history and resets the old run before connecting a switched run", async () => {
    const runBHistory = deferred<EventEnvelope[]>();
    mockRunApis(async (runId) =>
      runId === "run_a" ? [event("run_a", 80)] : runBHistory.promise,
    );

    class FakeEventSource {
      static instances: FakeEventSource[] = [];
      onopen: (() => void) | null = null;
      onmessage: ((message: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      closed = false;

      constructor(public readonly url: string) {
        FakeEventSource.instances.push(this);
      }

      close() {
        this.closed = true;
      }
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    const activeRunId = ref("run_a");
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: { render: () => h("div") } },
        { path: "/runs", component: { render: () => h("div") } },
        { path: "/runs/:runId", component: { render: () => h("div") } },
      ],
    });
    await router.push("/");
    await router.isReady();

    const container = document.createElement("div");
    const app = createApp({
      render: () => h(RunDetailView, { runId: activeRunId.value }),
    });
    app.use(createPinia()).use(router);

    try {
      app.mount(container);
      await vi.waitFor(() => {
        expect(FakeEventSource.instances).toHaveLength(1);
      });
      expect(FakeEventSource.instances[0].url).toContain(
        "/runs/run_a/stream?after_sequence=80",
      );
      expect(container.textContent).toContain("Goal run_a");

      activeRunId.value = "run_b";
      await nextTick();
      await vi.waitFor(() => {
        expect(FakeEventSource.instances[0].closed).toBe(true);
      });
      expect(FakeEventSource.instances).toHaveLength(1);
      expect(container.textContent).not.toContain("Goal run_a");

      runBHistory.resolve([event("run_b", 2)]);
      await vi.waitFor(() => {
        expect(FakeEventSource.instances).toHaveLength(2);
      });
      expect(FakeEventSource.instances[1].url).toContain(
        "/runs/run_b/stream?after_sequence=2",
      );
      expect(container.textContent).toContain("Goal run_b");
    } finally {
      app.unmount();
    }
  });
});
