<script setup lang="ts">
import { computed } from "vue";
import { Check, Clock, LoaderCircle, Terminal, X } from "@lucide/vue";

const props = defineProps<{
  toolName: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  resultPreview?: string;
  durationMs?: number | null;
  agentName?: string | null;
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

const durationLabel = computed(() => {
  const ms = props.durationMs;
  if (ms == null) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
});

// 参数条目：字符串原样展示，其他类型 JSON 格式化
const argEntries = computed(() =>
  Object.entries(props.args ?? {}).map(([key, value]) => ({
    key,
    value: typeof value === "string" ? value : JSON.stringify(value, null, 2),
  })),
);

const isError = computed(() => props.status === "error");
const isRunning = computed(() => props.status === "running");
</script>

<template>
  <div class="tool-call-card" :data-status="status">
    <!-- 头部：图标 + 工具名 + 状态 + 耗时（一行，不显示 agent 标签） -->
    <div class="tool-head">
      <span class="tool-icon"><Terminal :size="13" /></span>
      <code class="tool-name">{{ toolName }}</code>
      <component
        :is="statusIcon"
        :size="13"
        :class="{ spin: isRunning }"
      />
      <span v-if="durationLabel" class="tool-duration">{{ durationLabel }}</span>
      <span v-if="isRunning" class="tool-running-label">执行中</span>
    </div>
    <!-- 参数：直接展示 key-value，不用点击展开 -->
    <div v-if="argEntries.length" class="tool-args">
      <div v-for="entry in argEntries" :key="entry.key" class="tool-arg">
        <span class="tool-arg-key">{{ entry.key }}</span>
        <pre class="tool-arg-value">{{ entry.value }}</pre>
      </div>
    </div>
    <!-- 结果：直接展示，error 时红色背景 -->
    <div v-if="resultPreview" class="tool-result" :class="{ error: isError }">
      <pre class="tool-result-text">{{ resultPreview }}</pre>
    </div>
  </div>
</template>
