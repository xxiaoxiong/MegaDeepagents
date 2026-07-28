<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AlertTriangle,
  ArrowUpRight,
  Bot,
  Braces,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  Clock3,
  FileCode2,
  Gauge,
  GitFork,
  MessageSquare,
  Network,
  Radio,
  Send,
  Sparkles,
  Square,
  Wrench,
} from "@lucide/vue";
import { api } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
import type {
  AgentExecution,
  EventEnvelope,
  RunExecution,
  TaskExecution,
} from "@/types";

const props = defineProps<{
  runId: string;
  execution: RunExecution | null;
  initialAgentId?: string | null;
}>();
const emit = defineEmits<{
  refresh: [];
  focusAgent: [agentId: string | null];
  focusTask: [taskId: string | null];
  openArtifact: [artifactId: string];
}>();

const selectedAgentId = ref("");
const message = ref("");
const working = ref(false);
const actionError = ref("");

const agents = computed(() => props.execution?.agents ?? []);
const tasks = computed(() => props.execution?.tasks ?? []);
const summary = computed(() => props.execution?.summary ?? null);
const attention = computed(() => props.execution?.attention ?? []);

watch(
  [agents, () => props.initialAgentId],
  ([items, initialAgentId]) => {
    if (
      initialAgentId &&
      items.some((item) => item.agent_id === initialAgentId)
    ) {
      selectedAgentId.value = initialAgentId;
      return;
    }
    if (items.some((item) => item.agent_id === selectedAgentId.value)) return;
    selectedAgentId.value =
      items.find((item) => ["running", "claiming"].includes(item.status))
        ?.agent_id ??
      items[0]?.agent_id ??
      "";
  },
  { immediate: true },
);

const selectedAgent = computed<AgentExecution | null>(
  () =>
    agents.value.find((item) => item.agent_id === selectedAgentId.value) ??
    null,
);
const selectedTasks = computed(() => {
  const agent = selectedAgent.value;
  if (!agent) return [];
  const ids = new Set([
    ...agent.assigned_task_ids,
    ...(agent.current_task_id ? [agent.current_task_id] : []),
  ]);
  return tasks.value.filter((task) => ids.has(task.task_id));
});

const activeCount = computed(
  () =>
    agents.value.filter((agent) =>
      ["running", "claiming", "stopping"].includes(agent.status),
    ).length,
);

const completedRatio = (agent: AgentExecution) => {
  if (!agent.assigned_task_ids.length) return 0;
  return Math.round(
    (agent.completed_task_ids.length / agent.assigned_task_ids.length) * 100,
  );
};

const compactDuration = (milliseconds: number) => {
  const seconds = Math.max(0, Math.round(milliseconds / 1_000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return `${hours}h ${remainder}m`;
};

const eventLabel = (event: EventEnvelope) => {
  const type = event.event_type.replace(/^root_graph:/, "");
  const labels: Record<string, string> = {
    TaskClaimed: "认领任务",
    TaskStarted: "开始执行",
    TaskProduced: "提交产出",
    TaskCompleted: "验证通过",
    TaskFailed: "执行失败",
    TaskRetryScheduled: "安排重试",
    BeforeToolUse: "调用工具",
    AfterToolUse: "工具完成",
    VerificationStarted: "开始验证",
    VerificationCompleted: "验证完成",
    AgentMessage: "团队协作消息",
    agent_spawned: "加入团队",
    assistant_message: "思考输出",
  };
  return labels[type] ?? type.replace(/_/g, " ");
};

const eventSummary = (event: EventEnvelope) => {
  const payload = event.payload ?? {};
  for (const key of [
    "message",
    "summary",
    "error",
    "reason",
    "tool",
    "tool_name",
    "objective",
    "verdict",
  ]) {
    const value = payload[key];
    if (value === undefined || value === null || value === "") continue;
    return typeof value === "string" ? value : JSON.stringify(value);
  }
  return event.task_id ? `Task ${event.task_id}` : "状态已持久化";
};

const eventIcon = (event: EventEnvelope) => {
  const type = event.event_type.toLowerCase();
  if (type.includes("tool")) return Wrench;
  if (type.includes("failed") || type.includes("error")) return AlertTriangle;
  if (type.includes("completed") || type.includes("verified")) return CheckCircle2;
  if (type.includes("message")) return MessageSquare;
  return CircleDot;
};

const time = (value?: string | null) => {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
};

function selectAgent(agentId: string) {
  selectedAgentId.value = agentId;
  emit("focusAgent", agentId);
}

function selectTask(task: TaskExecution) {
  emit("focusTask", task.task_id);
}

async function send() {
  const agent = selectedAgent.value;
  if (!agent || !message.value.trim()) return;
  working.value = true;
  actionError.value = "";
  try {
    await api.sendAgentMessage(props.runId, agent.agent_id, message.value.trim());
    message.value = "";
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error);
  } finally {
    working.value = false;
  }
}

async function stop() {
  const agent = selectedAgent.value;
  if (!agent) return;
  working.value = true;
  actionError.value = "";
  try {
    await api.stopAgent(props.runId, agent.agent_id);
    emit("refresh");
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error);
  } finally {
    working.value = false;
  }
}
</script>

<template>
  <section class="panel execution-workbench">
    <header class="workbench-heading">
      <div>
        <span class="eyebrow">Multi-agent mission control</span>
        <h2><Network :size="19" /> Agent 执行工作台</h2>
        <p>按 Agent 重放任务、工具、协作与交付链路；指标由持久事件实时推导。</p>
      </div>
      <div v-if="summary" class="workbench-live">
        <Radio :size="14" />
        <strong>{{ activeCount }} / {{ agents.length }}</strong>
        <span>Agent 活跃</span>
      </div>
    </header>

    <EmptyState
      v-if="!execution || !agents.length"
      title="正在建立 Agent 执行视图"
      description="团队创建后，每位 Agent 的详细步骤、工具与产出会在这里独立呈现。"
    />

    <template v-else>
      <div class="workbench-metrics">
        <div>
          <Gauge :size="15" />
          <span>并行倍率</span>
          <strong>{{ summary?.parallelism.toFixed(2) }}×</strong>
          <small>
            {{ Math.round((summary?.utilization ?? 0) * 100) }}% 利用率 ·
            峰值 {{ summary?.peak_concurrency }} 并发
          </small>
        </div>
        <div>
          <Clock3 :size="15" />
          <span>可观测工时</span>
          <strong>{{ compactDuration(summary?.active_time_ms ?? 0) }}</strong>
          <small>墙钟 {{ compactDuration(summary?.wall_time_ms ?? 0) }}</small>
        </div>
        <div>
          <Wrench :size="15" />
          <span>工具调用</span>
          <strong>{{ summary?.tool_call_count }}</strong>
          <small>{{ summary?.handoff_count }} 次协作交接</small>
        </div>
        <div>
          <GitFork :size="15" />
          <span>关键路径</span>
          <strong>{{ summary?.critical_path_remaining }} 步</strong>
          <small>{{ summary?.retry_count }} 次修复 / 重试</small>
        </div>
      </div>

      <div v-if="attention.length" class="workbench-attention">
        <button
          v-for="item in attention.slice(0, 4)"
          :key="`${item.kind}-${item.task_id}-${item.agent_id}`"
          :data-severity="item.severity"
          @click="
            item.agent_id
              ? selectAgent(item.agent_id)
              : item.task_id
                ? emit('focusTask', item.task_id)
                : undefined
          "
        >
          <AlertTriangle :size="14" />
          <span><strong>{{ item.title }}</strong>{{ item.detail }}</span>
          <ChevronRight v-if="item.agent_id || item.task_id" :size="14" />
        </button>
      </div>

      <nav class="agent-lanes" aria-label="Agent 执行通道">
        <button
          v-for="agent in agents"
          :key="agent.agent_id"
          :class="{ active: selectedAgentId === agent.agent_id }"
          :data-status="agent.status"
          @click="selectAgent(agent.agent_id)"
        >
          <span class="lane-avatar"><Bot :size="16" /></span>
          <span class="lane-copy">
            <span><strong>{{ agent.name }}</strong><StatusBadge :status="agent.status" /></span>
            <small>{{ agent.current_task_title || agent.latest_summary }}</small>
          </span>
          <span class="lane-progress">
            <i :style="{ width: `${completedRatio(agent)}%` }" />
          </span>
          <span class="lane-count">
            {{ agent.completed_task_ids.length }}/{{ agent.assigned_task_ids.length }}
          </span>
        </button>
      </nav>

      <div v-if="selectedAgent" class="agent-focus">
        <aside class="agent-focus-summary">
          <div class="agent-identity">
            <span class="agent-identity-icon"><Sparkles :size="19" /></span>
            <div>
              <span class="eyebrow">{{ selectedAgent.role }}</span>
              <h3>{{ selectedAgent.name }}</h3>
            </div>
            <StatusBadge :status="selectedAgent.status" />
          </div>

          <div class="agent-now">
            <span>当前工作</span>
            <strong>
              {{ selectedAgent.current_task_title || "当前没有占用任务" }}
            </strong>
            <small v-if="selectedAgent.current_task_id">
              {{ selectedAgent.current_task_id }}
            </small>
            <p>{{ selectedAgent.latest_summary }}</p>
          </div>

          <dl class="agent-focus-stats">
            <div><dt>审计事件</dt><dd>{{ selectedAgent.event_count }}</dd></div>
            <div><dt>工具调用</dt><dd>{{ selectedAgent.tool_call_count }}</dd></div>
            <div><dt>完成任务</dt><dd>{{ selectedAgent.completed_task_ids.length }}</dd></div>
            <div><dt>交付物</dt><dd>{{ selectedAgent.artifact_ids.length }}</dd></div>
          </dl>

          <div class="capability-cloud">
            <span
              v-for="capability in selectedAgent.capabilities"
              :key="capability"
            >
              {{ capability }}
            </span>
          </div>

          <div class="agent-directive">
            <label><MessageSquare :size="14" /> 向此 Agent 注入上下文</label>
            <textarea
              v-model="message"
              rows="3"
              placeholder="补充约束、验收标准或新的线索…"
              @keydown.ctrl.enter="send"
            />
            <small v-if="actionError" class="action-error">{{ actionError }}</small>
            <div>
              <button
                class="btn btn-ghost btn-small danger"
                :disabled="
                  working ||
                  ['stopped', 'failed'].includes(selectedAgent.status)
                "
                @click="stop"
              >
                <Square :size="12" /> 停止
              </button>
              <button
                class="btn btn-primary btn-small"
                :disabled="working || !message.trim()"
                @click="send"
              >
                <Send :size="12" /> 发送
              </button>
            </div>
          </div>
        </aside>

        <section class="agent-activity">
          <header>
            <div>
              <span class="eyebrow">Agent trace</span>
              <h3>详细执行过程</h3>
            </div>
            <small>最近活动 {{ time(selectedAgent.last_activity_at) }}</small>
          </header>
          <ol v-if="selectedAgent.recent_events.length">
            <li
              v-for="event in selectedAgent.recent_events"
              :key="event.event_id"
            >
              <span class="agent-event-icon">
                <component :is="eventIcon(event)" :size="13" />
              </span>
              <span class="agent-event-copy">
                <span>
                  <strong>{{ eventLabel(event) }}</strong>
                  <time>{{ time(event.timestamp) }}</time>
                </span>
                <p>{{ eventSummary(event) }}</p>
                <button
                  v-if="event.task_id"
                  @click="emit('focusTask', event.task_id)"
                >
                  {{ event.task_id }} <ArrowUpRight :size="11" />
                </button>
              </span>
            </li>
          </ol>
          <EmptyState
            v-else
            title="尚无执行步骤"
            description="Agent 开始认领任务后会形成独立、可回放的活动轨迹。"
          />
        </section>

        <aside class="agent-delivery">
          <header>
            <span class="eyebrow">Ownership & delivery</span>
            <h3>任务与产出</h3>
          </header>
          <div class="agent-task-list">
            <button
              v-for="task in selectedTasks"
              :key="task.task_id"
              :class="{ critical: task.critical }"
              @click="selectTask(task)"
            >
              <span>
                <Braces :size="13" />
                {{ task.task_id }}
                <i v-if="task.critical">关键路径</i>
              </span>
              <strong>{{ task.title }}</strong>
              <small v-if="task.blocked_by.length">
                等待 {{ task.blocked_by.join("、") }}
              </small>
              <StatusBadge :status="task.status" />
            </button>
            <p v-if="!selectedTasks.length" class="agent-delivery-empty">
              当前没有可归属的任务。
            </p>
          </div>
          <div class="agent-artifact-list">
            <button
              v-for="artifactId in selectedAgent.artifact_ids"
              :key="artifactId"
              @click="emit('openArtifact', artifactId)"
            >
              <FileCode2 :size="14" />
              <span>{{ artifactId }}</span>
              <ArrowUpRight :size="12" />
            </button>
            <p v-if="!selectedAgent.artifact_ids.length">
              Artifact 生成后会在这里直接交付。
            </p>
          </div>
        </aside>
      </div>
    </template>
  </section>
</template>
