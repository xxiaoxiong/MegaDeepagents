import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";
import { api } from "@/lib/api";
import type { AgentRun, EventEnvelope, RunSnapshot } from "@/types";

const emptySnapshot = (): RunSnapshot => ({
  run: null,
  tasks: [],
  agents: [],
  artifacts: [],
  events: [],
  permissions: [],
  plans: [],
  graph: null,
  errors: {},
  git: {},
  diagnostics: null,
  execution: null,
});

function mergeEvents(
  snapshotEvents: EventEnvelope[],
  liveEvents: EventEnvelope[],
): EventEnvelope[] {
  const byId = new Map<string, EventEnvelope>();
  for (const event of [...snapshotEvents, ...liveEvents]) {
    byId.set(event.event_id, event);
  }
  return [...byId.values()].sort(
    (left, right) =>
      left.sequence - right.sequence ||
      left.event_id.localeCompare(right.event_id),
  );
}

export type LiveRefreshScope =
  | "run"
  | "tasks"
  | "agents"
  | "artifacts"
  | "approvals"
  | "errors"
  | "git"
  | "diagnostics"
  | "execution";

export const useRunsStore = defineStore("runs", () => {
  const runs = ref<AgentRun[]>([]);
  const current = reactive<RunSnapshot>(emptySnapshot());
  const loading = ref(false);
  const error = ref("");
  const eventIds = new Set<string>();
  let loadRunGeneration = 0;
  let liveRefreshGeneration = 0;
  const keyRefreshGeneration = new Map<keyof RunSnapshot, number>();
  const lastSequence = computed(
    () => current.events.at(-1)?.sequence ?? 0,
  );

  async function loadRuns() {
    loading.value = true;
    error.value = "";
    try {
      runs.value = await api.listRuns();
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason);
      throw reason;
    } finally {
      loading.value = false;
    }
  }

  async function loadRun(runId: string) {
    const generation = ++loadRunGeneration;
    loading.value = true;
    error.value = "";
    try {
      const [
        run,
        tasks,
        agents,
        artifacts,
        events,
        permissions,
        plans,
        graph,
        errors,
        git,
        diagnostics,
        execution,
      ] = await Promise.all([
        api.getRun(runId),
        api.listTasks(runId),
        api.listAgents(runId),
        api.listArtifacts(runId),
        api.listAllEvents(runId),
        api.listPermissions(runId),
        api.listPlans(runId),
        api.taskGraph(runId),
        api.errors(runId),
        api.git(runId),
        api.diagnostics(runId),
        api.execution(runId).catch(() => null),
      ]);
      if (generation !== loadRunGeneration) return;
      const liveEvents =
        current.run?.run_id === runId ||
        (current.run === null &&
          current.events.every((event) => event.run_id === runId))
          ? [...current.events]
          : [];
      const mergedEvents = mergeEvents(events, liveEvents);
      eventIds.clear();
      for (const event of mergedEvents) eventIds.add(event.event_id);
      Object.assign(current, {
        run,
        tasks,
        agents,
        artifacts,
        events: mergedEvents,
        permissions,
        plans,
        graph,
        errors,
        git,
        diagnostics,
        execution,
      });
    } catch (reason) {
      if (generation !== loadRunGeneration) return;
      error.value = reason instanceof Error ? reason.message : String(reason);
      throw reason;
    } finally {
      if (generation === loadRunGeneration) loading.value = false;
    }
  }

  function applyEvent(event: EventEnvelope) {
    if (eventIds.has(event.event_id)) return;
    eventIds.add(event.event_id);
    const tail = current.events.at(-1);
    if (!tail || tail.sequence < event.sequence) {
      current.events.push(event);
    } else {
      let low = 0;
      let high = current.events.length;
      while (low < high) {
        const middle = (low + high) >>> 1;
        if (current.events[middle].sequence < event.sequence) low = middle + 1;
        else high = middle;
      }
      current.events.splice(low, 0, event);
    }
    if (current.events.length > 20_000) {
      const removed = current.events.splice(0, 2_000);
      for (const item of removed) eventIds.delete(item.event_id);
    }
  }

  async function refreshLiveData(
    runId: string,
    scopes?: LiveRefreshScope[],
  ) {
    const generation = ++liveRefreshGeneration;
    const requested = new Set<LiveRefreshScope>(
      scopes ?? [
        "run",
        "tasks",
        "agents",
        "artifacts",
        "approvals",
        "errors",
        "git",
        "diagnostics",
        "execution",
      ],
    );
    const updates: Partial<RunSnapshot> = {};
    const jobs: Promise<void>[] = [];
    const load = <K extends keyof RunSnapshot>(
      key: K,
      promise: Promise<RunSnapshot[K]>,
    ) => {
      keyRefreshGeneration.set(key, generation);
      jobs.push(promise.then((value) => {
        updates[key] = value;
      }));
    };
    if (requested.has("run")) load("run", api.getRun(runId));
    if (requested.has("tasks")) {
      load("tasks", api.listTasks(runId));
      load("graph", api.taskGraph(runId));
    }
    if (requested.has("agents")) load("agents", api.listAgents(runId));
    if (requested.has("artifacts")) {
      load("artifacts", api.listArtifacts(runId));
    }
    if (requested.has("approvals")) {
      load("permissions", api.listPermissions(runId));
      load("plans", api.listPlans(runId));
    }
    if (requested.has("errors")) load("errors", api.errors(runId));
    if (requested.has("git")) load("git", api.git(runId));
    if (requested.has("diagnostics")) {
      load("diagnostics", api.diagnostics(runId));
    }
    if (requested.has("execution")) {
      load(
        "execution",
        api.execution(runId).catch(() => current.execution),
      );
    }
    await Promise.all(jobs);
    if (current.run?.run_id !== runId) return;
    const accepted: Partial<RunSnapshot> = {};
    for (const key of Object.keys(updates) as Array<keyof RunSnapshot>) {
      if (keyRefreshGeneration.get(key) === generation) {
        Object.assign(accepted, { [key]: updates[key] });
      }
    }
    Object.assign(current, accepted);
  }

  function resetCurrent() {
    loadRunGeneration += 1;
    liveRefreshGeneration += 1;
    keyRefreshGeneration.clear();
    eventIds.clear();
    Object.assign(current, emptySnapshot());
  }

  return {
    runs,
    current,
    loading,
    error,
    lastSequence,
    loadRuns,
    loadRun,
    applyEvent,
    refreshLiveData,
    resetCurrent,
  };
});
