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
});

export const useRunsStore = defineStore("runs", () => {
  const runs = ref<AgentRun[]>([]);
  const current = reactive<RunSnapshot>(emptySnapshot());
  const loading = ref(false);
  const error = ref("");
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
      ] = await Promise.all([
        api.getRun(runId),
        api.listTasks(runId),
        api.listAgents(runId),
        api.listArtifacts(runId),
        api.listEvents(runId),
        api.listPermissions(runId),
        api.listPlans(runId),
        api.taskGraph(runId),
        api.errors(runId),
        api.git(runId),
      ]);
      Object.assign(current, {
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
      });
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason);
      throw reason;
    } finally {
      loading.value = false;
    }
  }

  function applyEvent(event: EventEnvelope) {
    if (current.events.some((item) => item.event_id === event.event_id)) return;
    current.events.push(event);
    current.events.sort((a, b) => a.sequence - b.sequence);
    if (current.events.length > 2_000) current.events.splice(0, 500);
  }

  async function refreshLiveData(runId: string) {
    const [run, tasks, agents, artifacts, permissions, plans] = await Promise.all([
      api.getRun(runId),
      api.listTasks(runId),
      api.listAgents(runId),
      api.listArtifacts(runId),
      api.listPermissions(runId),
      api.listPlans(runId),
    ]);
    Object.assign(current, { run, tasks, agents, artifacts, permissions, plans });
  }

  function resetCurrent() {
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
