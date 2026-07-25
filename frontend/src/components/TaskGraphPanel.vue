<script setup lang="ts">
import { computed } from "vue";
import { ArrowRight, GitBranch, Network, Route } from "@lucide/vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
import type { Task, TaskGraph } from "@/types";

const props = defineProps<{ tasks: Task[]; graph: TaskGraph | null }>();

const taskMap = computed(() =>
  Object.fromEntries(props.tasks.map((task) => [task.task_id, task])),
);

function taskDepth(taskId: string, trail = new Set<string>()): number {
  if (trail.has(taskId)) return 0;
  const task = taskMap.value[taskId];
  if (!task?.dependencies.length) return 0;
  const next = new Set(trail);
  next.add(taskId);
  return (
    1 +
    Math.max(
      0,
      ...task.dependencies.map((dependency) => taskDepth(dependency, next)),
    )
  );
}

const levels = computed(() => {
  const grouped = new Map<number, Task[]>();
  for (const task of props.tasks) {
    const depth = taskDepth(task.task_id);
    grouped.set(depth, [...(grouped.get(depth) ?? []), task]);
  }
  return [...grouped.entries()]
    .sort(([a], [b]) => a - b)
    .map(([depth, tasks]) => ({
      depth,
      tasks: tasks.sort(
        (a, b) =>
          Number(b.task_id === props.graph?.root_task_id) -
            Number(a.task_id === props.graph?.root_task_id) ||
          a.task_id.localeCompare(b.task_id),
      ),
    }));
});

const stats = computed(() => ({
  completed: props.tasks.filter((task) => task.status === "succeeded").length,
  active: props.tasks.filter((task) =>
    ["claimed", "running", "produced", "verifying"].includes(task.status),
  ).length,
  blocked: props.tasks.filter((task) =>
    ["failed", "blocked", "repair_required", "replan_required"].includes(
      task.status,
    ),
  ).length,
}));

const formatRetry = (value?: string | null) => {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
};
</script>

<template>
  <section class="panel graph-panel">
    <header class="panel-heading graph-heading">
      <div>
        <span class="eyebrow">Main problem → dynamic subproblems</span>
        <h2><Network :size="19" /> 主线与动态子任务</h2>
        <p>按依赖层级展示主问题、并行分支、验证与修复任务。</p>
      </div>
      <div class="graph-head-meta">
        <span><b>{{ stats.completed }}</b> 已完成</span>
        <span><b>{{ stats.active }}</b> 执行中</span>
        <span :class="{ warn: stats.blocked }"><b>{{ stats.blocked }}</b> 需处理</span>
        <span class="graph-version"><GitBranch :size="14" /> v{{ graph?.version ?? 0 }}</span>
      </div>
    </header>

    <EmptyState
      v-if="!levels.length"
      title="正在生成任务主线"
      description="Supervisor 完成拆解后，主问题与动态子问题会立即出现在这里。"
    />
    <div v-else class="task-graph-scroll">
      <div class="task-levels">
        <template v-for="(level, levelIndex) in levels" :key="level.depth">
          <section class="task-level">
            <header>
              <span>阶段 {{ String(level.depth + 1).padStart(2, "0") }}</span>
              <small>{{ level.tasks.length }} 个可并行节点</small>
            </header>
            <div class="task-level-nodes">
              <article
                v-for="task in level.tasks"
                :key="task.task_id"
                class="task-node"
                :class="{ root: task.task_id === graph?.root_task_id }"
                :data-status="task.status"
              >
                <div class="task-node-top">
                  <span class="task-id">
                    {{ task.task_id }}
                    <i v-if="task.task_id === graph?.root_task_id">MAIN</i>
                  </span>
                  <StatusBadge :status="task.status" />
                </div>
                <h3>{{ task.title || task.objective }}</h3>
                <p>{{ task.objective }}</p>
                <div v-if="task.last_error" class="task-error">{{ task.last_error }}</div>
                <footer>
                  <span v-if="task.dependencies.length" class="dependency-list">
                    <Route :size="12" /> {{ task.dependencies.join(" · ") }}
                  </span>
                  <span v-else>主线起点</span>
                  <span>{{ task.attempts }}/{{ task.max_attempts }} 次尝试</span>
                </footer>
                <small v-if="task.next_attempt_at" class="retry-at">
                  自动重试 · {{ formatRetry(task.next_attempt_at) }}
                </small>
              </article>
            </div>
          </section>
          <div v-if="levelIndex < levels.length - 1" class="level-link">
            <ArrowRight :size="18" />
          </div>
        </template>
      </div>
    </div>
  </section>
</template>
