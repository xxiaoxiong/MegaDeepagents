<script setup lang="ts">
import { computed, ref } from "vue";
import {
  Activity,
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Copy,
  Pause,
  Play,
  Radio,
  RotateCcw,
  Search,
  ShieldAlert,
  SlidersHorizontal,
  Wrench,
} from "@lucide/vue";
import EmptyState from "@/components/EmptyState.vue";
import type { EventEnvelope } from "@/types";

const props = defineProps<{
  events: EventEnvelope[];
  connected?: boolean;
  streamState?: string;
  streamError?: string;
  agentFilter?: string | null;
  taskFilter?: string | null;
}>();
const emit = defineEmits<{ clearFocus: [] }>();

type EventCategory =
  | "all"
  | "execution"
  | "tools"
  | "communication"
  | "recovery"
  | "errors";
const query = ref("");
const category = ref<EventCategory>("all");
const expanded = ref(new Set<string>());
const copied = ref("");
const paused = ref(false);
const showNoise = ref(false);
const displayLimit = ref(500);

const labels: Record<string, string> = {
  RunCreated: "运行已创建",
  RunStarted: "运行已启动",
  RunCompleted: "运行已完成",
  RunFailed: "运行失败",
  TaskCreated: "子任务已创建",
  TaskClaimed: "Agent 已认领任务",
  TaskStarted: "任务开始执行",
  TaskHeartbeat: "Agent 持续工作中",
  TaskProduced: "任务已产出",
  TaskCompleted: "任务验证通过",
  TaskFailed: "任务执行失败",
  TaskRetryScheduled: "已安排自动重试",
  TaskFailedPermanently: "重试预算已耗尽",
  TaskTimedOut: "任务执行超时",
  BeforeToolUse: "准备调用工具",
  AfterToolUse: "工具调用完成",
  VerificationStarted: "开始验证",
  VerificationCompleted: "验证完成",
  PermissionRequested: "等待权限审批",
  TeammateSpawned: "动态派生 Agent",
  AgentMessage: "Agent 消息",
  SchedulerStarted: "调度器已启动",
  SchedulerRoundStarted: "开始调度轮次",
  SchedulerStopped: "调度器已停止",
  RetryBackoffWaiting: "重试退避等待",
  ManualRetryRequested: "人工发起恢复",
};

const normalizedType = (event: EventEnvelope) =>
  event.event_type.replace(/^root_graph:/, "");

const prettyType = (event: EventEnvelope) => {
  const raw = normalizedType(event);
  return (
    labels[event.event_type] ??
    labels[raw] ??
    raw.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
};

const categoryOf = (event: EventEnvelope): Exclude<EventCategory, "all"> => {
  const type = normalizedType(event).toLowerCase();
  if (
    ["failed", "error", "timeout", "conflict", "denied"].some((token) =>
      type.includes(token),
    )
  )
    return "errors";
  if (
    ["retry", "repair", "replan", "resume", "backoff", "recovery"].some(
      (token) => type.includes(token),
    )
  )
    return "recovery";
  if (["tool", "permission", "approval"].some((token) => type.includes(token)))
    return "tools";
  if (
    ["message", "assistant", "handoff", "delegat"].some((token) =>
      type.includes(token),
    )
  )
    return "communication";
  return "execution";
};

const isNoise = (event: EventEnvelope) => {
  const type = normalizedType(event).replace(/_/g, "").toLowerCase();
  return [
    "assistanttoken",
    "taskheartbeat",
    "teammateheartbeat",
    "schedulerroundstarted",
    "toolcallstarted",
    "toolcallresult",
  ].includes(type);
};

const severityOf = (event: EventEnvelope) => {
  const type = normalizedType(event).toLowerCase();
  if (["failed", "error", "timeout", "conflict"].some((token) => type.includes(token)))
    return "error";
  if (
    ["retry", "repair", "replan", "waiting", "permission", "blocked"].some(
      (token) => type.includes(token),
    )
  )
    return "warning";
  if (["completed", "verified", "succeeded", "finalized"].some((token) => type.includes(token)))
    return "success";
  return "info";
};

const compactValue = (value: unknown): string => {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(compactValue).join("、");
  if (value && typeof value === "object") return JSON.stringify(value);
  return "";
};

const summaryOf = (event: EventEnvelope) => {
  const payload = event.payload ?? {};
  const preferred = [
    "message",
    "error",
    "summary",
    "reason",
    "feedback",
    "tool",
    "verdict",
    "action",
    "objective",
  ];
  for (const key of preferred) {
    if (payload[key] !== undefined && payload[key] !== null) {
      const value = compactValue(payload[key]);
      if (value) return value;
    }
  }
  const entries = Object.entries(payload).filter(([, value]) => value !== null);
  if (!entries.length) return "状态已持久化，等待下一步。";
  return entries
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${compactValue(value)}`)
    .join(" · ");
};

const time = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));

const fullTime = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(new Date(value));

const scopedEvents = computed(() =>
  props.events.filter(
    (event) =>
      (!props.agentFilter || event.agent_id === props.agentFilter) &&
      (!props.taskFilter || event.task_id === props.taskFilter),
  ),
);

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return [...scopedEvents.value]
    .reverse()
    .filter((event) => showNoise.value || !isNoise(event))
    .filter((event) => category.value === "all" || categoryOf(event) === category.value)
    .filter((event) => {
      if (!needle) return true;
      return [
        event.event_type,
        event.agent_id,
        event.task_id,
        summaryOf(event),
        JSON.stringify(event.payload),
      ].some((value) => String(value ?? "").toLowerCase().includes(needle));
    });
});

const visible = computed(() => filtered.value.slice(0, displayLimit.value));
const tickerEvents = computed(() =>
  [...scopedEvents.value]
    .reverse()
    .filter((event) => !isNoise(event))
    .slice(0, 8),
);
const counts = computed(() => ({
  execution: scopedEvents.value.filter((event) => categoryOf(event) === "execution").length,
  tools: scopedEvents.value.filter((event) => categoryOf(event) === "tools").length,
  communication: scopedEvents.value.filter(
    (event) => categoryOf(event) === "communication",
  ).length,
  recovery: scopedEvents.value.filter((event) => categoryOf(event) === "recovery").length,
  errors: scopedEvents.value.filter((event) => categoryOf(event) === "errors").length,
  noise: scopedEvents.value.filter(isNoise).length,
}));

function toggle(eventId: string) {
  const next = new Set(expanded.value);
  if (next.has(eventId)) next.delete(eventId);
  else next.add(eventId);
  expanded.value = next;
}

async function copyEvent(event: EventEnvelope) {
  await navigator.clipboard.writeText(JSON.stringify(event, null, 2));
  copied.value = event.event_id;
  window.setTimeout(() => {
    if (copied.value === event.event_id) copied.value = "";
  }, 1_500);
}
</script>

<template>
  <section class="panel audit-console">
    <header class="audit-header">
      <div>
        <span class="eyebrow">Live observability</span>
        <h2><Activity :size="19" /> 运行审计台</h2>
        <p>每一次编排、工具调用、心跳、失败与恢复都按顺序保留。</p>
      </div>
      <div class="audit-connection" :data-state="streamState || (connected ? 'live' : 'reconnecting')">
        <Radio :size="14" />
        <span>{{ connected ? "实时同步" : "自动重连中" }}</span>
        <small v-if="streamError">{{ streamError }}</small>
      </div>
    </header>

    <div v-if="tickerEvents.length" class="activity-ticker" aria-label="最新实时活动">
      <span class="ticker-label"><CircleDot :size="13" /> LIVE</span>
      <div class="ticker-viewport">
        <div class="ticker-track" :class="{ paused }">
          <span v-for="event in tickerEvents" :key="event.event_id">
            <b>#{{ event.sequence }}</b>
            {{ prettyType(event) }}
            <em>{{ summaryOf(event) }}</em>
          </span>
        </div>
      </div>
      <button
        class="ticker-toggle"
        :title="paused ? '继续滚动' : '暂停滚动'"
        @click="paused = !paused"
      >
        <Play v-if="paused" :size="13" />
        <Pause v-else :size="13" />
      </button>
    </div>

    <div class="audit-toolbar">
      <label class="audit-search">
        <Search :size="15" />
        <input v-model="query" type="search" placeholder="搜索事件、Agent、Task 或完整负载" />
      </label>
      <div class="audit-filters" aria-label="事件分类">
        <button :class="{ active: category === 'all' }" @click="category = 'all'">
          全部 <span>{{ scopedEvents.length }}</span>
        </button>
        <button :class="{ active: category === 'execution' }" @click="category = 'execution'">
          执行 <span>{{ counts.execution }}</span>
        </button>
        <button :class="{ active: category === 'tools' }" @click="category = 'tools'">
          工具 <span>{{ counts.tools }}</span>
        </button>
        <button
          :class="{ active: category === 'communication' }"
          @click="category = 'communication'"
        >
          协作 <span>{{ counts.communication }}</span>
        </button>
        <button :class="{ active: category === 'recovery' }" @click="category = 'recovery'">
          恢复 <span>{{ counts.recovery }}</span>
        </button>
        <button :class="{ active: category === 'errors' }" @click="category = 'errors'">
          异常 <span>{{ counts.errors }}</span>
        </button>
        <button :class="{ active: showNoise }" @click="showNoise = !showNoise">
          <SlidersHorizontal :size="11" />
          {{ showNoise ? "隐藏底层事件" : `原始事件 +${counts.noise}` }}
        </button>
      </div>
    </div>

    <div v-if="agentFilter || taskFilter" class="audit-focus-bar">
      <span>当前聚焦</span>
      <strong v-if="agentFilter">Agent · {{ agentFilter }}</strong>
      <strong v-if="taskFilter">Task · {{ taskFilter }}</strong>
      <button @click="emit('clearFocus')">查看全部事件</button>
    </div>

    <EmptyState
      v-if="!visible.length"
      title="暂无匹配事件"
      description="运行开始后，这里会逐条显示持久化审计记录。"
    />
    <ol v-else class="audit-list">
      <li
        v-for="event in visible"
        :key="event.event_id"
        class="audit-event"
        :data-severity="severityOf(event)"
      >
        <button class="audit-event-main" @click="toggle(event.event_id)">
          <span class="audit-expand">
            <ChevronDown v-if="expanded.has(event.event_id)" :size="15" />
            <ChevronRight v-else :size="15" />
          </span>
          <span class="audit-dot">
            <AlertTriangle v-if="severityOf(event) === 'error'" :size="14" />
            <RotateCcw v-else-if="categoryOf(event) === 'recovery'" :size="14" />
            <Wrench v-else-if="categoryOf(event) === 'tools'" :size="14" />
            <ShieldAlert v-else-if="severityOf(event) === 'warning'" :size="14" />
            <Check v-else-if="severityOf(event) === 'success'" :size="14" />
            <CircleDot v-else :size="14" />
          </span>
          <span class="audit-event-copy">
            <span class="audit-event-title">
              <b>#{{ event.sequence }}</b>
              <strong>{{ prettyType(event) }}</strong>
              <i>{{ categoryOf(event) }}</i>
            </span>
            <span class="audit-event-summary">{{ summaryOf(event) }}</span>
            <span class="audit-event-meta">
              <time :datetime="event.timestamp">{{ time(event.timestamp) }}</time>
              <span v-if="event.agent_id">Agent · {{ event.agent_id }}</span>
              <span v-if="event.task_id">Task · {{ event.task_id }}</span>
            </span>
          </span>
        </button>
        <div v-if="expanded.has(event.event_id)" class="audit-event-detail">
          <dl>
            <div><dt>事件 ID</dt><dd>{{ event.event_id }}</dd></div>
            <div><dt>完整时间</dt><dd>{{ fullTime(event.timestamp) }}</dd></div>
            <div v-if="event.trace_id"><dt>Trace ID</dt><dd>{{ event.trace_id }}</dd></div>
          </dl>
          <div class="payload-heading">
            <span>完整事件负载</span>
            <button class="btn btn-ghost btn-small" @click="copyEvent(event)">
              <Check v-if="copied === event.event_id" :size="13" />
              <Copy v-else :size="13" />
              {{ copied === event.event_id ? "已复制" : "复制 JSON" }}
            </button>
          </div>
          <pre><code>{{ JSON.stringify(event.payload, null, 2) }}</code></pre>
        </div>
      </li>
    </ol>

    <footer v-if="filtered.length" class="audit-footer">
      <span>显示 {{ Math.min(displayLimit, filtered.length) }} / {{ filtered.length }} 条</span>
      <button
        v-if="visible.length < filtered.length"
        class="btn btn-secondary btn-small"
        @click="displayLimit += 500"
      >
        加载更早的 500 条
      </button>
    </footer>
  </section>
</template>
