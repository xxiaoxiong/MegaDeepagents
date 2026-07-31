<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ArrowRight,
  Bot,
  FileArchive,
  GitFork,
  ListTree,
  LoaderCircle,
  MessageSquarePlus,
  PanelRight,
  Search,
  Send,
  Settings,
  Sparkles,
  Square,
  X,
} from "@lucide/vue";
import { api } from "@/lib/api";
import { useChatThread } from "@/composables/useChatThread";
import type {
  AgentRun,
  Artifact,
  ChatMessage,
  CreateRunInput,
  RunExecution,
  RunMode,
} from "@/types";
import StatusBadge from "@/components/StatusBadge.vue";
import ChatMessageItem from "@/components/chat/ChatMessageItem.vue";
import ArtifactExplorer from "@/components/ArtifactExplorer.vue";

const route = useRoute();
const router = useRouter();

// ===== 当前会话 =====
const propsRunId = computed(() => {
  const id = route.params.runId;
  return typeof id === "string" ? id : "";
});
const runIdRef = ref(propsRunId.value);
watch(propsRunId, (v) => {
  runIdRef.value = v;
});

const thread = useChatThread(runIdRef);

// ===== 侧边栏：运行列表 =====
const runs = ref<AgentRun[]>([]);
const runsLoading = ref(false);
const runsError = ref("");
const query = ref("");

async function loadRuns() {
  runsLoading.value = true;
  runsError.value = "";
  try {
    runs.value = await api.listRuns();
  } catch (e) {
    runsError.value = e instanceof Error ? e.message : String(e);
  } finally {
    runsLoading.value = false;
  }
}

const visibleRuns = computed(() => {
  const needle = query.value.toLowerCase().trim();
  const list = needle
    ? runs.value.filter(
        (r) =>
          r.goal.toLowerCase().includes(needle) ||
          r.run_id.toLowerCase().includes(needle) ||
          r.status.toLowerCase().includes(needle),
      )
    : runs.value;
  // 最近的在前
  return [...list].sort((a, b) =>
    String(b.updated_at ?? b.created_at ?? "").localeCompare(
      String(a.updated_at ?? a.created_at ?? ""),
    ),
  );
});

const currentRun = computed(() =>
  runs.value.find((r) => r.run_id === runIdRef.value) ?? null,
);
const execution = ref<RunExecution | null>(null);
let executionTimer: number | null = null;

// ===== 右侧产出文件抽屉（对齐 codex 聊天 + 文件面板体验）=====
const drawerOpen = ref(false);
const selectedArtifactId = ref<string | null>(null);
const artifacts = ref<Artifact[]>([]);
const artifactById = computed(
  () => new Map(artifacts.value.map((artifact) => [artifact.artifact_id, artifact])),
);

function artifactForMessage(message: ChatMessage): Artifact | null {
  if (!("type" in message) || message.type !== "artifact") return null;
  return artifactById.value.get(message.artifactId) ?? null;
}

async function loadArtifacts(id = runIdRef.value) {
  if (!id) {
    artifacts.value = [];
    return;
  }
  try {
    const list = await api.listArtifacts(id);
    if (runIdRef.value === id) artifacts.value = list;
  } catch {
    // 抽屉在产出投影暂时不可用时仍可用，仅保持旧列表
  }
}

function toggleDrawer() {
  drawerOpen.value = !drawerOpen.value;
  if (drawerOpen.value) void loadArtifacts();
}

function openArtifact(artifactId: string) {
  selectedArtifactId.value = artifactId;
  drawerOpen.value = true;
  if (!artifacts.value.length) void loadArtifacts();
}

async function loadExecution(id = runIdRef.value) {
  if (!id) {
    execution.value = null;
    return;
  }
  try {
    const result = await api.execution(id);
    if (runIdRef.value === id) execution.value = result;
  } catch {
    // The chat stream remains usable when the optional operator projection
    // is temporarily unavailable.
  }
}

function scheduleExecutionRefresh() {
  if (!runIdRef.value) return;
  if (executionTimer !== null) window.clearTimeout(executionTimer);
  executionTimer = window.setTimeout(() => {
    executionTimer = null;
    void loadExecution();
  }, 700);
}

watch(
  runIdRef,
  (id) => {
    execution.value = null;
    artifacts.value = [];
    selectedArtifactId.value = null;
    void loadExecution(id);
    if (drawerOpen.value) void loadArtifacts(id);
  },
  { immediate: true },
);
watch(() => thread.messages.value.length, () => {
  scheduleExecutionRefresh();
  // 产出文件随事件增长：抽屉打开时同步刷新，关闭时不浪费请求
  if (drawerOpen.value) void loadArtifacts();
});

// Artifact cards show filenames rather than opaque IDs even while the drawer
// is closed.  Fetch metadata as soon as replay/live events reveal an unknown
// artifact; content is still loaded lazily only after the user opens it.
watch(
  () =>
    thread.messages.value
      .filter((message) => "type" in message && message.type === "artifact")
      .map((message) =>
        "type" in message && message.type === "artifact" ? message.artifactId : "",
      )
      .filter(Boolean)
      .join("|"),
  (ids) => {
    if (!ids) return;
    const hasMissing = ids.split("|").some((id) => !artifactById.value.has(id));
    if (hasMissing) void loadArtifacts();
  },
  { immediate: true },
);

const activeAgentCount = computed(
  () =>
    execution.value?.agents.filter((agent) =>
      ["claiming", "planning", "running", "working", "testing", "reviewing"].includes(
        agent.status.toLowerCase(),
      ),
    ).length ?? 0,
);

function openAgent(agentId: string) {
  void router.push({
    path: `/runs/${runIdRef.value}`,
    query: { agent: agentId },
    hash: "#execution-workbench",
  });
}

// ===== 输入框 =====
const draft = ref("");
const sending = ref(false);
const sendError = ref("");
const composerRef = ref<HTMLTextAreaElement | null>(null);
const threadScrollRef = ref<HTMLElement | null>(null);

const canSend = computed(
  () => draft.value.trim().length >= 4 && !sending.value,
);

function autoGrow() {
  const el = composerRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
}

watch(draft, () => nextTick(autoGrow));

async function scrollToBottom(force = false) {
  await nextTick();
  const el = threadScrollRef.value;
  if (!el) return;
  const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
  if (force || distance < 120) {
    el.scrollTop = el.scrollHeight;
  }
}

watch(
  () => thread.messages.value.length,
  () => scrollToBottom(false),
);

// 助手流式时持续贴底
watch(
  () =>
    thread.messages.value
      .map((m) => ("role" in m && m.role === "assistant" ? m.content : ""))
      .join("|"),
  () => {
    const el = threadScrollRef.value;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distance < 160) el.scrollTop = el.scrollHeight;
  },
);

async function startNewChat() {
  // 进入空白对话页（不创建 Run，等用户输入第一条消息再创建）
  if (route.params.runId) {
    await router.push("/chat");
  }
  draft.value = "";
  sendError.value = "";
  await nextTick(() => composerRef.value?.focus());
}

async function send() {
  if (!canSend.value) return;
  const content = draft.value.trim();
  draft.value = "";
  sendError.value = "";
  await nextTick(autoGrow);

  sending.value = true;
  try {
    if (runIdRef.value) {
      // 已有 Run：把消息追加到对话流
      await api.sendRunMessage(runIdRef.value, content);
    } else {
      // 新对话：用第一条消息作为 Run 的 goal，创建后跳转
      const payload: CreateRunInput = {
        goal: content,
        mode: "auto" as RunMode,
        team_template: "software_dev_team",
        repository_path: null,
        base_branch: null,
        review_required: true,
        auto_approve_low_risk: false,
        metadata: {},
      };
      const run = await api.createRun(payload);
      await loadRuns();
      await router.push(`/chat/${run.run_id}`);
    }
    await scrollToBottom(true);
  } catch (e) {
    sendError.value = e instanceof Error ? e.message : String(e);
    // 失败时把消息还回输入框
    if (!draft.value) draft.value = content;
  } finally {
    sending.value = false;
  }
}

function onComposerKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    void send();
  }
}

// ===== 流式状态文案 =====
const streamStateLabel = computed(() => {
  switch (thread.state.value) {
    case "live":
      return "实时";
    case "reconnecting":
      return "重连中";
    default:
      return "离线";
  }
});

// ===== 生命周期 =====
onMounted(async () => {
  await loadRuns();
  await scrollToBottom(true);
});
onBeforeUnmount(() => {
  if (executionTimer !== null) window.clearTimeout(executionTimer);
});

// 路由切到 /chat（空白）时清空选择
watch(
  () => route.path,
  async (p) => {
    if (p === "/chat") {
      await nextTick(() => composerRef.value?.focus());
    }
  },
);

// 快捷示例
const examples = [
  "审计这个仓库的认证模块，列出高风险问题。",
  "为 utils/logging.py 补齐单元测试，覆盖率到 85%。",
  "把 README 翻译成英文，并补充部署章节。",
];
</script>

<template>
  <div class="chat-layout" :class="{ 'has-drawer': drawerOpen && runIdRef }">
    <!-- 侧边栏：会话列表 -->
    <aside class="chat-sidebar">
      <RouterLink class="chat-brand" to="/chat">
        <span class="brand-mark"><Sparkles :size="18" /></span>
        <span>
          <strong>MegaDeepagents</strong>
          <small>Agent Control Plane</small>
        </span>
      </RouterLink>

      <button class="chat-new-btn" type="button" @click="startNewChat">
        <MessageSquarePlus :size="14" /> 新对话
      </button>

      <label class="chat-search">
        <Search :size="14" />
        <input v-model="query" type="search" placeholder="搜索目标或 Run ID" />
      </label>

      <div v-if="runsLoading && !runs.length" class="chat-history-loading">
        <LoaderCircle class="spin" :size="14" /> 读取中…
      </div>
      <div v-else-if="runsError" class="chat-history-loading">
        {{ runsError }}
      </div>

      <nav v-else class="chat-thread-list" aria-label="会话列表">
        <button
          v-for="run in visibleRuns"
          :key="run.run_id"
          type="button"
          class="chat-thread-item"
          :class="{ active: run.run_id === runIdRef }"
          @click="router.push(`/chat/${run.run_id}`)"
        >
          <span class="thread-title">{{ run.goal || run.run_id }}</span>
          <span class="thread-meta">
            <StatusBadge :status="run.status" />
            <span>{{ run.resolved_mode || run.mode }}</span>
          </span>
        </button>
        <div v-if="!visibleRuns.length" class="chat-history-loading">
          暂无运行记录
        </div>
      </nav>

      <div class="chat-sidebar-foot">
        <RouterLink to="/runs">
          <ListTree :size="15" /> 运行任务
        </RouterLink>
        <RouterLink to="/settings">
          <Settings :size="15" /> 系统设置
        </RouterLink>
      </div>
    </aside>

    <!-- 主区：对话窗口 -->
    <main class="chat-main">
      <header v-if="runIdRef" class="chat-header">
        <div class="chat-header-title">
          <span class="eyebrow">Run {{ runIdRef }}</span>
          <h2>{{ currentRun?.goal ?? "对话中" }}</h2>
        </div>
        <div class="chat-header-actions">
          <span
            class="chat-stream-state"
            :data-state="thread.state.value"
            :title="thread.lastError.value || ''"
          >
            {{ streamStateLabel }}
          </span>
          <button
            v-if="runIdRef"
            class="btn btn-ghost btn-small"
            :class="{ active: drawerOpen }"
            type="button"
            :title="drawerOpen ? '收起产出文件面板' : '查看产出文件'"
            @click="toggleDrawer"
          >
            <FileArchive :size="14" /> 文件
            <span v-if="artifacts.length" class="chat-drawer-badge">{{ artifacts.length }}</span>
          </button>
          <RouterLink
            class="btn btn-ghost btn-small"
            :to="`/runs/${runIdRef}`"
            title="查看编排仪表盘"
          >
            <PanelRight :size="14" /> 高级视图
          </RouterLink>
          <button
            v-if="currentRun && !['succeeded', 'failed', 'cancelled'].includes(currentRun.status)"
            class="btn btn-ghost btn-small danger"
            type="button"
            @click="api.controlRun(runIdRef, 'cancel').then(loadRuns)"
          >
            <Square :size="13" /> 取消
          </button>
        </div>
      </header>

      <section
        v-if="runIdRef && execution?.agents.length"
        class="chat-team-pulse"
      >
        <div class="chat-team-pulse-head">
          <span><GitFork :size="13" /> Agent 团队实时协作</span>
          <small>
            <template v-if="activeAgentCount > 0">{{ activeAgentCount }} 个 Agent 并行中 · </template>
            {{ execution.summary.peak_concurrency }} 峰值并发 ·
            {{ execution.summary.tool_call_count }} 次工具调用
          </small>
        </div>
        <div class="chat-team-pulse-agents">
          <button
            v-for="agent in execution.agents"
            :key="agent.agent_id"
            type="button"
            @click="openAgent(agent.agent_id)"
          >
            <span class="chat-agent-avatar"><Bot :size="13" /></span>
            <span>
              <strong>{{ agent.name }}</strong>
              <small>{{ agent.current_task_title || agent.latest_summary }}</small>
            </span>
            <StatusBadge :status="agent.status" />
          </button>
        </div>
        <button
          v-if="execution.attention.length"
          type="button"
          class="chat-team-attention"
          @click="router.push(`/runs/${runIdRef}`)"
        >
          {{ execution.attention[0].title }}
          <ArrowRight :size="12" />
        </button>
      </section>

      <!-- 对话流 -->
      <div
        v-if="runIdRef"
        ref="threadScrollRef"
        class="chat-thread"
      >
        <div v-if="thread.loadingHistory.value" class="chat-history-loading">
          <LoaderCircle class="spin" :size="16" /> 正在回放历史事件…
        </div>
        <div v-else-if="thread.error.value" class="chat-history-loading">
          {{ thread.error.value }}
        </div>

        <div class="chat-thread-inner">
          <ChatMessageItem
            v-for="msg in thread.messages.value"
            :key="msg.id"
            :message="msg"
            :run-id="runIdRef"
            :artifact="artifactForMessage(msg)"
            @open-artifact="openArtifact"
          />
          <div v-if="!thread.messages.value.length && !thread.loadingHistory.value" class="chat-history-loading">
            还没有事件，向 Agent 发送一条消息开始对话。
          </div>
        </div>
      </div>

      <!-- 空会话欢迎页 -->
      <div v-else class="chat-empty">
        <div class="chat-empty-card">
          <span class="brand-mark" style="margin: 0 auto;"><Sparkles :size="22" /></span>
          <h3>跟你的 Agent 团队对话</h3>
          <p>
            描述你想完成的事，Root Graph 会自动选择单 Agent 或团队路径。
            执行过程中的工具调用、产出与审批都会实时出现在这里。
          </p>
          <div class="chat-empty-examples">
            <button
              v-for="ex in examples"
              :key="ex"
              type="button"
              class="chat-empty-example"
              @click="draft = ex; composerRef?.focus()"
            >
              {{ ex }}
              <ArrowRight :size="13" />
            </button>
          </div>
        </div>
      </div>

      <!-- 输入框 -->
      <div class="chat-composer">
        <div class="chat-composer-inner">
          <textarea
            ref="composerRef"
            v-model="draft"
            rows="1"
            :placeholder="
              runIdRef
                ? '继续对话，或追加指令…（Enter 发送，Shift+Enter 换行）'
                : '描述你想完成的事，将作为新运行的目标…（Enter 发送）'
            "
            @input="autoGrow"
            @keydown="onComposerKey"
          />
          <button
            class="chat-send"
            type="button"
            :disabled="!canSend"
            :title="sending ? '发送中…' : '发送（Enter）'"
            @click="send"
          >
            <LoaderCircle v-if="sending" class="spin" :size="16" />
            <Send v-else :size="16" />
          </button>
        </div>
        <div v-if="sendError" class="chat-composer-hint" style="color: var(--danger);">
          {{ sendError }}
        </div>
        <div v-else class="chat-composer-hint">
          MegaDeepagents · 受治理的 Agent 运行时 · 高风险操作仍需人工审批
        </div>
      </div>
    </main>

    <!-- 右侧产出文件抽屉：复用 ArtifactExplorer，不跳转、就地预览 -->
    <aside v-if="drawerOpen && runIdRef" class="chat-drawer">
      <header class="chat-drawer-head">
        <strong><FileArchive :size="15" /> 产出文件</strong>
        <button
          class="btn btn-ghost btn-small"
          type="button"
          title="收起"
          @click="drawerOpen = false"
        >
          <X :size="14" />
        </button>
      </header>
      <ArtifactExplorer
        :run-id="runIdRef"
        :artifacts="artifacts"
        :initial-artifact-id="selectedArtifactId"
        compact
        @selected="(id) => (selectedArtifactId = id)"
      />
    </aside>
  </div>
</template>
