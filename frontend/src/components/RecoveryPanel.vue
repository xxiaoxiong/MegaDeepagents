<script setup lang="ts">
import { computed, ref } from "vue";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  RotateCcw,
  ShieldAlert,
  Wrench,
} from "@lucide/vue";
import { api } from "@/lib/api";
import EmptyState from "@/components/EmptyState.vue";
import type { RunDiagnostics, Task } from "@/types";

const props = defineProps<{
  runId: string;
  diagnostics: RunDiagnostics | null;
  errors: Record<string, unknown>;
}>();
const emit = defineEmits<{ refresh: [] }>();
const working = ref("");
const notice = ref("");
const rawOpen = ref(false);

const tasks = computed(
  () => (Array.isArray(props.errors.tasks) ? props.errors.tasks : []) as Task[],
);
const errorEvents = computed(() =>
  Array.isArray(props.errors.events) ? props.errors.events : [],
);
const retryable = computed(() => props.diagnostics?.retryable_task_ids ?? []);

async function retry(taskId?: string) {
  working.value = taskId || "all";
  notice.value = "";
  try {
    const result = await api.retryRun(props.runId, {
      ...(taskId ? { task_id: taskId } : {}),
      reason: taskId ? "operator_retry_task" : "operator_retry_failed_tasks",
    });
    notice.value = `已恢复 ${result.retried_task_ids.length} 个任务，运行正在继续。`;
    emit("refresh");
  } catch (reason) {
    notice.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    working.value = "";
  }
}
</script>

<template>
  <section class="recovery-panel">
    <header class="recovery-heading">
      <div>
        <span class="eyebrow">Recovery center</span>
        <h3><Wrench :size="17" /> 错误诊断与恢复</h3>
        <p>错误不会被吞掉；每次失败的原因、尝试次数和恢复动作都在这里。</p>
      </div>
      <button
        v-if="retryable.length"
        class="btn btn-primary"
        :disabled="Boolean(working)"
        @click="retry()"
      >
        <RotateCcw :class="{ spin: working === 'all' }" :size="15" />
        重试全部失败任务
      </button>
    </header>

    <div v-if="notice" class="notice" :class="{ error: !notice.startsWith('已恢复') }">
      {{ notice }}
    </div>

    <EmptyState
      v-if="!tasks.length && !diagnostics?.blockers.length"
      title="当前没有错误"
      description="运行期间的失败与自动重试记录仍可在审计台中查看。"
    >
      <CheckCircle2 :size="18" />
    </EmptyState>

    <div v-else class="recovery-list">
      <article v-for="task in tasks" :key="task.task_id" class="recovery-card">
        <div class="recovery-card-icon">
          <AlertTriangle v-if="task.status === 'failed'" :size="18" />
          <ShieldAlert v-else :size="18" />
        </div>
        <div class="recovery-card-copy">
          <div>
            <span class="task-id">{{ task.task_id }}</span>
            <span class="recovery-status">{{ task.status }}</span>
          </div>
          <h4>{{ task.title || task.objective }}</h4>
          <p>{{ task.last_error || "任务需要控制面介入。" }}</p>
          <small>已尝试 {{ task.attempts }} / {{ task.max_attempts }} 次</small>
        </div>
        <button
          v-if="retryable.includes(task.task_id)"
          class="btn btn-secondary btn-small"
          :disabled="Boolean(working)"
          @click="retry(task.task_id)"
        >
          <RotateCcw :class="{ spin: working === task.task_id }" :size="13" />
          重试此任务
        </button>
      </article>
    </div>

    <button v-if="errorEvents.length" class="raw-error-toggle" @click="rawOpen = !rawOpen">
      <ChevronDown :class="{ rotated: rawOpen }" :size="15" />
      {{ rawOpen ? "收起" : "查看" }} {{ errorEvents.length }} 条原始错误事件
    </button>
    <pre v-if="rawOpen" class="json-view"><code>{{ JSON.stringify(errorEvents, null, 2) }}</code></pre>
  </section>
</template>
