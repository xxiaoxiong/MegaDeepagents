<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRef, watch } from "vue";
import {
  AlertTriangle,
  ArrowLeft,
  Boxes,
  Braces,
  FileArchive,
  GitCommit,
  LoaderCircle,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  Send,
  ShieldCheck,
  Square,
} from "@lucide/vue";
import { api } from "@/lib/api";
import { useRunStream } from "@/composables/useRunStream";
import { useRunsStore } from "@/stores/runs";
import StatusBadge from "@/components/StatusBadge.vue";
import TaskGraphPanel from "@/components/TaskGraphPanel.vue";
import AgentPanel from "@/components/AgentPanel.vue";
import EventTimeline from "@/components/EventTimeline.vue";
import ApprovalQueue from "@/components/ApprovalQueue.vue";
import ArtifactExplorer from "@/components/ArtifactExplorer.vue";

const props = defineProps<{ runId: string }>();
const store = useRunsStore();
const activeTab = ref("artifacts");
const controlling = ref(false);
const runMessage = ref("");
const pageError = ref("");
const refreshTimer = ref<number | null>(null);
const streamRunId = toRef(props, "runId");
const sequence = computed(() => store.lastSequence);

const stream = useRunStream(streamRunId, sequence, (event) => {
  store.applyEvent(event);
  window.clearTimeout(refreshTimer.value ?? undefined);
  refreshTimer.value = window.setTimeout(() => store.refreshLiveData(props.runId), 250);
});

const progress = computed(() => {
  const total = store.current.tasks.length;
  const done = store.current.tasks.filter((task) => task.status === "succeeded").length;
  return { total, done, percentage: total ? Math.round((done / total) * 100) : 0 };
});

const terminal = computed(() =>
  ["succeeded", "failed", "cancelled"].includes(store.current.run?.status ?? ""),
);

async function refresh() {
  pageError.value = "";
  try {
    await store.loadRun(props.runId);
  } catch (reason) {
    pageError.value = reason instanceof Error ? reason.message : String(reason);
  }
}

async function control(action: "pause" | "resume" | "cancel") {
  controlling.value = true;
  try {
    await api.controlRun(props.runId, action);
    await refresh();
  } finally {
    controlling.value = false;
  }
}

async function sendRunMessage() {
  if (!runMessage.value.trim()) return;
  controlling.value = true;
  try {
    await api.sendRunMessage(props.runId, runMessage.value.trim());
    runMessage.value = "";
  } finally {
    controlling.value = false;
  }
}

watch(() => props.runId, refresh);
onMounted(refresh);
onBeforeUnmount(() => {
  store.resetCurrent();
  if (refreshTimer.value) window.clearTimeout(refreshTimer.value);
});
</script>

<template>
  <div class="page detail-page">
    <div v-if="store.loading && !store.current.run" class="page-loading full">
      <LoaderCircle class="spin" :size="25" /> 正在恢复运行视图…
    </div>
    <template v-else-if="store.current.run">
      <header class="detail-header">
        <div class="detail-heading">
          <RouterLink class="back-link" to="/runs"><ArrowLeft :size="15" /> 运行列表</RouterLink>
          <div class="detail-title-line">
            <StatusBadge :status="store.current.run.status" />
            <span class="run-id">{{ store.current.run.run_id }}</span>
            <span class="stream-state" :data-live="stream.connected.value">
              {{ stream.connected.value ? "LIVE" : "RECONNECTING" }}
            </span>
          </div>
          <h1>{{ store.current.run.goal }}</h1>
        </div>
        <div class="detail-controls">
          <button class="btn btn-secondary" :disabled="store.loading" @click="refresh">
            <RefreshCw :class="{ spin: store.loading }" :size="15" /> 刷新
          </button>
          <button
            v-if="store.current.run.status === 'running'"
            class="btn btn-secondary"
            :disabled="controlling"
            @click="control('pause')"
          >
            <Pause :size="15" /> 暂停
          </button>
          <button
            v-if="['paused', 'waiting_human'].includes(store.current.run.status)"
            class="btn btn-primary"
            :disabled="controlling"
            @click="control('resume')"
          >
            <Play :size="15" /> 恢复
          </button>
          <button
            v-if="!terminal"
            class="btn btn-ghost danger"
            :disabled="controlling"
            @click="control('cancel')"
          >
            <Square :size="14" /> 取消
          </button>
        </div>
      </header>

      <div v-if="pageError" class="notice error">{{ pageError }}</div>

      <section class="run-overview">
        <div class="overview-stat">
          <span>执行模式</span>
          <strong>{{ store.current.run.resolved_mode || store.current.run.mode }}</strong>
          <small>{{ store.current.run.team_template }}</small>
        </div>
        <div class="overview-stat">
          <span>任务进度</span>
          <strong>{{ progress.done }} / {{ progress.total }}</strong>
          <div class="progress-track"><i :style="{ width: `${progress.percentage}%` }" /></div>
        </div>
        <div class="overview-stat">
          <span>Agent</span>
          <strong>{{ store.current.agents.length }}</strong>
          <small>{{ store.current.agents.filter((a) => a.status === "running").length }} 正在执行</small>
        </div>
        <div class="overview-stat">
          <span>Artifact</span>
          <strong>{{ store.current.artifacts.length }}</strong>
          <small>{{ store.current.permissions.length + store.current.plans.length }} 项待审批</small>
        </div>
      </section>

      <section class="detail-workspace">
        <TaskGraphPanel :tasks="store.current.tasks" :graph="store.current.graph" />
        <AgentPanel
          :run-id="runId"
          :agents="store.current.agents"
          @refresh="store.refreshLiveData(runId)"
        />
      </section>

      <section class="panel run-message">
        <label>
          <MessageSquare :size="16" />
          向整个团队广播消息
        </label>
        <input
          v-model="runMessage"
          placeholder="补充全局约束或新的上下文…"
          @keydown.enter="sendRunMessage"
        />
        <button class="btn btn-primary" :disabled="!runMessage.trim() || controlling" @click="sendRunMessage">
          <Send :size="14" /> 广播
        </button>
      </section>

      <section class="panel detail-bottom">
        <nav class="tab-nav" aria-label="运行数据">
          <button :class="{ active: activeTab === 'artifacts' }" @click="activeTab = 'artifacts'">
            <FileArchive :size="15" /> Artifacts <span>{{ store.current.artifacts.length }}</span>
          </button>
          <button :class="{ active: activeTab === 'approvals' }" @click="activeTab = 'approvals'">
            <ShieldCheck :size="15" /> 审批
            <span>{{ store.current.permissions.length + store.current.plans.length }}</span>
          </button>
          <button :class="{ active: activeTab === 'events' }" @click="activeTab = 'events'">
            <Braces :size="15" /> 事件 <span>{{ store.current.events.length }}</span>
          </button>
          <button :class="{ active: activeTab === 'git' }" @click="activeTab = 'git'">
            <GitCommit :size="15" /> Git
          </button>
          <button :class="{ active: activeTab === 'errors' }" @click="activeTab = 'errors'">
            <AlertTriangle :size="15" /> 错误
          </button>
        </nav>
        <div class="tab-content">
          <ArtifactExplorer
            v-if="activeTab === 'artifacts'"
            :run-id="runId"
            :artifacts="store.current.artifacts"
          />
          <ApprovalQueue
            v-else-if="activeTab === 'approvals'"
            :run-id="runId"
            :permissions="store.current.permissions"
            :plans="store.current.plans"
            @refresh="store.refreshLiveData(runId)"
          />
          <EventTimeline
            v-else-if="activeTab === 'events'"
            :events="store.current.events"
            :connected="stream.connected.value"
          />
          <pre v-else-if="activeTab === 'git'" class="json-view"><code>{{ JSON.stringify(store.current.git, null, 2) }}</code></pre>
          <pre v-else class="json-view"><code>{{ JSON.stringify(store.current.errors, null, 2) }}</code></pre>
        </div>
      </section>
    </template>
    <div v-else class="not-found">
      <Boxes :size="34" />
      <h1>未找到这个运行</h1>
      <p>{{ pageError || "运行可能不存在，或 API 服务当前不可用。" }}</p>
      <RouterLink class="btn btn-primary" to="/runs">返回运行列表</RouterLink>
    </div>
  </div>
</template>
