<script setup lang="ts">
import { computed } from "vue";
import { ArrowDown, GitBranch } from "@lucide/vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
import type { Task, TaskGraph } from "@/types";

const props = defineProps<{ tasks: Task[]; graph: TaskGraph | null }>();

const orderedTasks = computed(() => {
  if (!props.graph?.nodes || !Object.keys(props.graph.nodes).length)
    return props.tasks;
  const nodes = props.graph.nodes;
  return [...props.tasks].sort((a, b) => {
    if (a.task_id === props.graph?.root_task_id) return -1;
    if (b.task_id === props.graph?.root_task_id) return 1;
    const aDepth = Array.isArray(nodes[a.task_id]?.dependencies)
      ? (nodes[a.task_id].dependencies as unknown[]).length
      : a.dependencies.length;
    const bDepth = Array.isArray(nodes[b.task_id]?.dependencies)
      ? (nodes[b.task_id].dependencies as unknown[]).length
      : b.dependencies.length;
    return aDepth - bDepth;
  });
});
</script>

<template>
  <section class="panel graph-panel">
    <header class="panel-heading">
      <div>
        <span class="eyebrow">TaskGraph</span>
        <h2>执行计划</h2>
      </div>
      <span class="graph-version">
        <GitBranch :size="15" />
        v{{ graph?.version ?? 0 }}
      </span>
    </header>

    <EmptyState
      v-if="!orderedTasks.length"
      title="计划尚未生成"
      description="Supervisor 完成拆解后，任务依赖会显示在这里。"
    />
    <div v-else class="task-flow">
      <template v-for="(task, index) in orderedTasks" :key="task.task_id">
        <article class="task-node" :data-status="task.status">
          <div class="task-node-top">
            <span class="task-id">{{ task.task_id }}</span>
            <StatusBadge :status="task.status" />
          </div>
          <h3>{{ task.title || task.objective }}</h3>
          <p>{{ task.objective }}</p>
          <footer>
            <span v-if="task.dependencies.length">
              依赖 {{ task.dependencies.join(" · ") }}
            </span>
            <span v-else>根任务</span>
            <span>{{ task.attempts }}/{{ task.max_attempts }} 次尝试</span>
          </footer>
        </article>
        <div v-if="index < orderedTasks.length - 1" class="flow-link">
          <ArrowDown :size="16" />
        </div>
      </template>
    </div>
  </section>
</template>
