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
        diagnostics,
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
        diagnostics,
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
    if (current.events.length > 20_000) current.events.splice(0, 2_000);
  }

  async function refreshLiveData(runId: string) {
    const [
      run,
      tasks,
      agents,
      artifacts,
      permissions,
      plans,
      graph,
      errors,
      diagnostics,
    ] = await Promise.all([
      api.getRun(runId),
      api.listTasks(runId),
      api.listAgents(runId),
      api.listArtifacts(runId),
      api.listPermissions(runId),
      api.listPlans(runId),
      api.taskGraph(runId),
      api.errors(runId),
      api.diagnostics(runId),
    ]);
    Object.assign(current, {
      run,
      tasks,
      agents,
      artifacts,
      permissions,
      plans,
      graph,
      errors,
      diagnostics,
    });
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
