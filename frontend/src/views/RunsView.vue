<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import {
  ArrowRight,
  Bot,
  CalendarClock,
  ListChecks,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RotateCw,
  Search,
  Square,
} from "@lucide/vue";
import { api } from "@/lib/api";
import { useRunsStore } from "@/stores/runs";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";

const store = useRunsStore();
const router = useRouter();
const summary = reactive<Record<string, { agents: number; done: number; total: number }>>(
  {},
);
const query = ref("");
const working = reactive<Record<string, boolean>>({});

const visibleRuns = computed(() => {
  const needle = query.value.toLowerCase().trim();
  if (!needle) return store.runs;
  return store.runs.filter(
    (run) =>
      run.goal.toLowerCase().includes(needle) ||
      run.run_id.toLowerCase().includes(needle) ||
      run.status.toLowerCase().includes(needle),
  );
});

const formatTime = (value?: string) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(value))
    : "—";

async function load() {
  await store.loadRuns();
  await Promise.allSettled(
    store.runs.slice(0, 20).map(async (run) => {
      const [agents, tasks] = await Promise.all([
        api.listAgents(run.run_id),
        api.listTasks(run.run_id),
      ]);
      summary[run.run_id] = {
        agents: agents.length,
        done: tasks.filter((task) => task.status === "succeeded").length,
        total: tasks.length,
      };
    }),
  );
}

async function control(runId: string, action: "pause" | "resume" | "cancel") {
  working[runId] = true;
  try {
    await api.controlRun(runId, action);
    await load();
  } finally {
    working[runId] = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page runs-page">
    <header class="page-header">
      <div>
        <span class="eyebrow">Operations</span>
        <h1>运行任务</h1>
        <p>创建、观察并治理单 Agent 与多 Agent 的完整执行周期。</p>
      </div>
      <RouterLink class="btn btn-primary btn-large" to="/runs/new">
        <Plus :size="17" />
        创建运行
      </RouterLink>
    </header>

    <section class="runs-toolbar">
      <label class="search-box">
        <Search :size="17" />
        <input v-model="query" type="search" placeholder="搜索目标、Run ID 或状态" />
      </label>
      <button class="btn btn-secondary" :disabled="store.loading" @click="load">
        <RotateCw :class="{ spin: store.loading }" :size="15" />
        刷新
      </button>
    </section>

    <div v-if="store.error" class="notice error">{{ store.error }}</div>
    <div v-if="store.loading && !store.runs.length" class="page-loading">
      <LoaderCircle class="spin" :size="24" /> 正在读取运行记录…
    </div>
    <EmptyState
      v-else-if="!visibleRuns.length"
      title="还没有运行记录"
      description="创建第一个任务，让 Root Graph 自动选择单 Agent 或团队路径。"
    >
      <RouterLink class="btn btn-primary" to="/runs/new">
        <Plus :size="15" /> 创建第一个运行
      </RouterLink>
    </EmptyState>

    <div v-else class="run-list">
      <article v-for="run in visibleRuns" :key="run.run_id" class="run-card">
        <button class="run-card-main" @click="router.push(`/runs/${run.run_id}`)">
          <div class="run-card-title">
            <StatusBadge :status="run.status" />
            <span class="run-id">{{ run.run_id }}</span>
          </div>
          <h2>{{ run.goal }}</h2>
          <div class="run-meta">
            <span><Bot :size="14" /> {{ run.resolved_mode || run.mode }}</span>
            <span><CalendarClock :size="14" /> {{ formatTime(run.updated_at || run.created_at) }}</span>
            <span>
              <ListChecks :size="14" />
              {{ summary[run.run_id]?.done ?? 0 }}/{{ summary[run.run_id]?.total ?? 0 }} 任务
            </span>
            <span>{{ summary[run.run_id]?.agents ?? 0 }} Agents</span>
          </div>
        </button>
        <div class="run-card-actions">
          <button
            v-if="run.status === 'running'"
            class="icon-button"
            title="暂停"
            :disabled="working[run.run_id]"
            @click="control(run.run_id, 'pause')"
          >
            <Pause :size="16" />
          </button>
          <button
            v-if="['paused', 'waiting_human'].includes(run.status)"
            class="icon-button"
            title="恢复"
            :disabled="working[run.run_id]"
            @click="control(run.run_id, 'resume')"
          >
            <Play :size="16" />
          </button>
          <button
            v-if="!['succeeded', 'failed', 'cancelled'].includes(run.status)"
            class="icon-button danger"
            title="取消"
            :disabled="working[run.run_id]"
            @click="control(run.run_id, 'cancel')"
          >
            <Square :size="15" />
          </button>
          <button class="icon-button" title="查看详情" @click="router.push(`/runs/${run.run_id}`)">
            <ArrowRight :size="17" />
          </button>
        </div>
      </article>
    </div>
  </div>
</template>
