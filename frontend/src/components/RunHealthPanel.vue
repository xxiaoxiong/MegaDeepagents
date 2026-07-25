<script setup lang="ts">
import { computed } from "vue";
import {
  Activity,
  AlertOctagon,
  CheckCircle2,
  Clock3,
  HeartPulse,
  ShieldAlert,
  TimerReset,
} from "@lucide/vue";
import type { RunDiagnostics } from "@/types";

const props = defineProps<{
  diagnostics: RunDiagnostics | null;
  connected?: boolean;
}>();
const emit = defineEmits<{ recover: [] }>();

const label = computed(
  () =>
    ({
      healthy: "运行健康",
      attention: "需要处理",
      stalled: "疑似卡住",
      failed: "运行失败",
      completed: "运行完成",
    })[props.diagnostics?.health ?? "healthy"],
);

const silence = computed(() => {
  const value = props.diagnostics?.silence_seconds;
  if (value === null || value === undefined) return "尚无活动";
  if (value < 60) return `${Math.round(value)} 秒前`;
  if (value < 3_600) return `${Math.round(value / 60)} 分钟前`;
  return `${(value / 3_600).toFixed(1)} 小时前`;
});

const canRecover = computed(
  () => (props.diagnostics?.retryable_task_ids.length ?? 0) > 0,
);
</script>

<template>
  <section class="panel health-panel" :data-health="diagnostics?.health ?? 'healthy'">
    <header>
      <div class="health-title">
        <span class="health-icon">
          <CheckCircle2 v-if="diagnostics?.health === 'completed'" :size="18" />
          <AlertOctagon v-else-if="diagnostics?.health === 'failed'" :size="18" />
          <ShieldAlert v-else-if="['attention', 'stalled'].includes(diagnostics?.health ?? '')" :size="18" />
          <HeartPulse v-else :size="18" />
        </span>
        <div>
          <span class="eyebrow">Runtime health</span>
          <h2>{{ label }}</h2>
        </div>
      </div>
      <i class="health-pulse" />
    </header>

    <p class="health-recommendation">
      {{ diagnostics?.recommended_action ?? "正在读取运行诊断…" }}
    </p>

    <div class="health-metrics">
      <div>
        <Activity :size="15" />
        <span><small>当前阶段</small><strong>{{ diagnostics?.phase || "初始化" }}</strong></span>
      </div>
      <div>
        <Clock3 :size="15" />
        <span><small>最近活动</small><strong>{{ silence }}</strong></span>
      </div>
      <div>
        <TimerReset :size="15" />
        <span>
          <small>重试队列</small>
          <strong>{{ diagnostics?.delayed_retries.length ?? 0 }} 项</strong>
        </span>
      </div>
    </div>

    <div class="health-foot">
      <span class="connection-chip" :data-live="connected">
        <i /> {{ connected ? "事件通道正常" : "事件通道恢复中" }}
      </span>
      <button v-if="canRecover" class="btn btn-secondary btn-small" @click="emit('recover')">
        <TimerReset :size="14" /> 恢复失败任务
      </button>
    </div>
  </section>
</template>
