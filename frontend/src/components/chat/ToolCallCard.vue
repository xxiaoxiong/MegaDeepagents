<script setup lang="ts">
import { computed, ref } from "vue";
import {
  Check,
  ChevronDown,
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
}>();

const expanded = ref(false);
const argsJson = computed(() =>
  JSON.stringify(props.args ?? {}, null, 2),
);

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
</script>

<template>
  <div class="tool-call-card" :data-status="status">
    <button class="tool-head" type="button" @click="expanded = !expanded">
      <span class="tool-icon"><Terminal :size="14" /></span>
      <component
        :is="statusIcon"
        :size="14"
        :class="{ spin: status === 'running' }"
      />
      <code class="tool-name">{{ toolName }}</code>
      <span v-if="agentName" class="tool-agent">{{ agentName }}</span>
      <span v-if="durationLabel" class="tool-duration">
        <Clock :size="11" /> {{ durationLabel }}
      </span>
      <component
        :is="expanded ? ChevronDown : ChevronRight"
        :size="14"
        class="tool-chevron"
      />
    </button>
    <div v-if="expanded" class="tool-detail">
      <div class="tool-section">
        <div class="tool-section-label">参数</div>
        <pre class="tool-json">{{ argsJson }}</pre>
      </div>
      <div v-if="resultPreview" class="tool-section">
        <div class="tool-section-label">结果</div>
        <pre class="tool-json">{{ resultPreview }}</pre>
      </div>
    </div>
  </div>
</template>
