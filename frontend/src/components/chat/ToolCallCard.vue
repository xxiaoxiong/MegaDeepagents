<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  Check,
  ChevronRight,
  Clock,
  LoaderCircle,
  Terminal,
  X,
} from "@lucide/vue";

const props = defineProps<{
  toolName: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  resultPreview?: string;
  durationMs?: number | null;
  agentName?: string | null;
  startedAt?: string | null;
}>();

const statusIcon = computed(() => {
  switch (props.status) {
    case "running":
      return LoaderCircle;
    case "ok":
      return Check;
    case "error":
      return X;
  }
});

const now = ref(Date.now());
let elapsedTimer: number | null = null;

function parseBackendTimestamp(value: string): number {
  // Persisted run timestamps are UTC but older rows use ``datetime.utcnow``
  // without a trailing timezone. Browsers otherwise interpret those rows as
  // local time (for example, +8 hours in China) and wildly overstate a live
  // tool's elapsed time.
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    ? value
    : `${value}Z`;
  return new Date(normalized).getTime();
}

const effectiveDuration = computed(() => {
  if (props.durationMs != null) return props.durationMs;
  if (props.status !== "running" || !props.startedAt) return null;
  const started = parseBackendTimestamp(props.startedAt);
  return Number.isFinite(started) ? Math.max(0, now.value - started) : null;
});

const durationLabel = computed(() => {
  const ms = effectiveDuration.value;
  if (ms == null) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
});

const argEntries = computed(() =>
  Object.entries(props.args ?? {}).map(([key, value]) => ({
    key,
    value: typeof value === "string" ? value : JSON.stringify(value, null, 2),
  })),
);

const argSummary = computed(() => {
  const first = argEntries.value[0];
  if (!first) return "";
  const compact = first.value.replace(/\s+/g, " ").trim();
  return `${first.key}=${compact}`.slice(0, 88);
});

const isError = computed(() => props.status === "error");
const isRunning = computed(() => props.status === "running");
const statusLabel = computed(() => {
  if (props.status === "running") return "工具执行中";
  if (props.status === "error") return "执行失败";
  return "已完成";
});

watch(
  () => props.status,
  (status) => {
    if (elapsedTimer !== null) {
      window.clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
    if (status === "running") {
      now.value = Date.now();
      elapsedTimer = window.setInterval(() => {
        now.value = Date.now();
      }, 1_000);
    }
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  if (elapsedTimer !== null) window.clearInterval(elapsedTimer);
});
</script>

<template>
  <details class="tool-call-card" :data-status="status">
    <summary
      class="tool-head"
      title="点击查看工具参数和返回结果；计时仅包含工具本身，不包含模型思考"
    >
      <span class="tool-icon"><Terminal :size="13" /></span>
      <span v-if="agentName" class="tool-agent-name">{{ agentName }}</span>
      <code class="tool-name">{{ toolName }}</code>
      <span v-if="argSummary" class="tool-arg-summary">{{ argSummary }}</span>
      <span class="tool-status">
        <component
          :is="statusIcon"
          :size="12"
          :class="{ spin: isRunning }"
        />
        {{ statusLabel }}
        <span v-if="durationLabel">· {{ durationLabel }}</span>
      </span>
      <ChevronRight class="tool-chevron" :size="13" />
    </summary>

    <div class="tool-details">
      <p class="tool-timing-note">
        <Clock :size="12" />
        此耗时从 Agent 真正进入工具边界开始，不包含模型生成和选择工具的时间。
      </p>
      <div v-if="argEntries.length" class="tool-args">
        <div v-for="entry in argEntries" :key="entry.key" class="tool-arg">
          <span class="tool-arg-key">{{ entry.key }}</span>
          <pre class="tool-arg-value">{{ entry.value }}</pre>
        </div>
      </div>
      <div v-if="resultPreview" class="tool-result" :class="{ error: isError }">
        <pre class="tool-result-text">{{ resultPreview }}</pre>
      </div>
      <p v-else-if="isRunning" class="tool-pending-detail">
        正在等待工具返回结果。
      </p>
    </div>
  </details>
</template>
