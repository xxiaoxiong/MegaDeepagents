<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRef, watch } from "vue";
import {
  ArrowLeft,
  Bot,
  Boxes,
  CircleGauge,
  FileArchive,
  GitCommit,
  Layers3,
  LoaderCircle,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  Send,
  ShieldCheck,
  Square,
  TimerReset,
  Workflow,
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
import RecoveryPanel from "@/components/RecoveryPanel.vue";
import RunHealthPanel from "@/components/RunHealthPanel.vue";

const props = defineProps<{ runId: string }>();
const store = useRunsStore();
const activeTab = ref("artifacts");
const controlling = ref(false);
const runMessage = ref("");
const pageError = ref("");
const actionNotice = ref("");
const refreshTimer = ref<number | null>(null);
const streamRunId = toRef(props, "runId");
const sequence = computed(() => store.lastSequence);

const stream = useRunStream(streamRunId, sequence, (event) => {
  store.applyEvent(event);
  window.clearTimeout(refreshTimer.value ?? undefined);
  refreshTimer.value = window.setTimeout(
    () => store.refreshLiveData(props.runId),
    350,
  );
});

const progress = computed(() => {
  const total = store.current.tasks.length;
  const done = store.current.tasks.filter((task) => task.status === "succeeded").length;
  const active = store.current.tasks.filter((task) =>
    ["claimed", "running", "produced", "verifying"].includes(task.status),
  ).length;
  return {
    total,
    done,
    active,
    percentage: total ? Math.round((done / total) * 100) : 0,
  };
});

const terminal = computed(() =>
  ["succeeded", "failed", "cancelled"].includes(store.current.run?.status ?? ""),
);
const pendingApprovals = computed(
  () => store.current.permissions.length + store.current.plans.length,
);
const latestEvent = computed(() => store.current.events.at(-1));
const latestSummary = computed(() => {
  const event = latestEvent.value;
  if (!event) return "等待运行生成第一条审计事件";
  const payload = event.payload ?? {};
  return String(
    payload.message ??
      payload.summary ??
      payload.error ??
      payload.reason ??
      event.event_type.replace(/^root_graph:/, "").replace(/_/g, " "),
  );
});

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
  pageError.value = "";
  try {
    await api.controlRun(props.runId, action);
    await refresh();
  } catch (reason) {
    pageError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    controlling.value = false;
  }
}

async function recoverFailed() {
  controlling.value = true;
  pageError.value = "";
  actionNotice.value = "";
  try {
    const result = await api.retryRun(props.runId, {
      reason: "operator_recovery_from_health_panel",
    });
    actionNotice.value = `已恢复 ${result.retried_task_ids.length} 个失败任务。`;
    await refresh();
  } catch (reason) {
    pageError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    controlling.value = false;
  }
}

async function sendRunMessage() {
  if (!runMessage.value.trim()) return;
  controlling.value = true;
  pageError.value = "";
  try {
    await api.sendRunMessage(props.runId, runMessage.value.trim());
    runMessage.value = "";
    actionNotice.value = "消息已写入团队信箱，将在下一个安全点生效。";
  } catch (reason) {
    pageError.value = reason instanceof Error ? reason.message : String(reason);
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
      <LoaderCircle class="spin" :size="25" /> 正在恢复完整运行现场…
    </div>
    <template v-else-if="store.current.run">
      <header class="detail-hero">
        <div class="detail-heading">
          <RouterLink class="back-link" to="/runs">
            <ArrowLeft :size="15" /> 运行列表
          </RouterLink>
          <div class="detail-kicker">
            <StatusBadge :status="store.current.run.status" />
            <span class="run-id">{{ store.current.run.run_id }}</span>
            <span class="stream-state" :data-live="stream.connected.value">
              <i />
              {{ stream.connected.value ? "LIVE AUDIT" : "RECOVERING" }}
            </span>
          </div>
          <h1>{{ store.current.run.goal }}</h1>
          <p class="detail-latest">
            <span>刚刚发生</span>
            {{ latestSummary }}
          </p>
        </div>
        <div class="detail-controls">
          <button class="btn btn-secondary" :disabled="store.loading" @click="refresh">
            <RefreshCw :class="{ spin: store.loading }" :size="15" /> 刷新快照
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
            v-if="store.current.run.status === 'failed'"
            class="btn btn-primary"
            :disabled="controlling || !store.current.diagnostics?.retryable_task_ids.length"
            @click="recoverFailed"
          >
            <TimerReset :size="15" /> 恢复失败任务
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
      <div v-if="actionNotice" class="notice success">{{ actionNotice }}</div>

      <section class="run-overview">
        <div class="overview-stat">
          <span class="overview-icon"><Workflow :size="18" /></span>
          <div>
            <span>执行模式</span>
            <strong>{{ store.current.run.resolved_mode || store.current.run.mode }}</strong>
            <small>{{ store.current.run.team_template }}</small>
          </div>
        </div>
        <div class="overview-stat progress-stat">
          <span class="overview-icon violet"><CircleGauge :size="18" /></span>
          <div>
            <span>任务进度</span>
            <strong>{{ progress.done }} / {{ progress.total }}</strong>
            <small>{{ progress.active }} 个任务正在执行</small>
            <div class="progress-track"><i :style="{ width: `${progress.percentage}%` }" /></div>
          </div>
        </div>
        <div class="overview-stat">
          <span class="overview-icon mint"><Bot :size="18" /></span>
          <div>
            <span>Agent 团队</span>
            <strong>{{ store.current.agents.length }}</strong>
            <small>{{ store.current.agents.filter((a) => a.status === "running").length }} 正在工作</small>
          </div>
        </div>
        <div class="overview-stat">
          <span class="overview-icon amber"><Layers3 :size="18" /></span>
          <div>
            <span>审计与产出</span>
            <strong>{{ store.current.diagnostics?.event_count ?? store.current.events.length }}</strong>
            <small>{{ store.current.artifacts.length }} Artifacts · {{ pendingApprovals }} 待审批</small>
          </div>
        </div>
      </section>

      <section class="detail-workspace">
        <TaskGraphPanel :tasks="store.current.tasks" :graph="store.current.graph" />
        <aside class="operations-rail">
          <RunHealthPanel
            :diagnostics="store.current.diagnostics"
            :connected="stream.connected.value"
            @recover="recoverFailed"
          />
          <AgentPanel
            :run-id="runId"
            :agents="store.current.agents"
            @refresh="store.refreshLiveData(runId)"
          />
        </aside>
      </section>

      <EventTimeline
        :events="store.current.events"
        :connected="stream.connected.value"
        :stream-state="stream.state.value"
        :stream-error="stream.lastError.value"
      />

      <section class="panel run-message">
        <div>
          <label><MessageSquare :size="16" /> 向整个团队广播消息</label>
          <small>消息会持久化，并在 Agent 的下一个安全点注入上下文。</small>
        </div>
        <input
          v-model="runMessage"
          placeholder="补充全局约束、验收标准或新的上下文…"
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
            <ShieldCheck :size="15" /> 审批 <span>{{ pendingApprovals }}</span>
          </button>
          <button :class="{ active: activeTab === 'recovery' }" @click="activeTab = 'recovery'">
            <TimerReset :size="15" /> 错误与恢复
            <span>{{ store.current.diagnostics?.blockers.length ?? 0 }}</span>
          </button>
          <button :class="{ active: activeTab === 'git' }" @click="activeTab = 'git'">
            <GitCommit :size="15" /> Git
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
          <RecoveryPanel
            v-else-if="activeTab === 'recovery'"
            :run-id="runId"
            :diagnostics="store.current.diagnostics"
            :errors="store.current.errors"
            @refresh="refresh"
          />
          <pre v-else class="json-view"><code>{{ JSON.stringify(store.current.git, null, 2) }}</code></pre>
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
