<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  Braces,
  Check,
  ChevronRight,
  FileText,
  LoaderCircle,
  Terminal,
  X,
} from "@lucide/vue";
import { api } from "@/lib/api";

const props = defineProps<{
  runId: string;
  toolName: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  resultPreview?: string;
  durationMs?: number | null;
  agentName?: string | null;
  startedAt?: string | null;
}>();

const statusIcon = computed(() => {
  if (props.status === "running") return LoaderCircle;
  if (props.status === "error") return X;
  return Check;
});

const now = ref(Date.now());
let elapsedTimer: number | null = null;
let detailAbort: AbortController | null = null;

function parseBackendTimestamp(value: string): number {
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
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

const normalizedToolName = computed(() => props.toolName.toLowerCase().replace(/[-.]/g, "_"));
const isFileRead = computed(() =>
  ["read_file", "readfile", "view_file", "open_file", "get_file_content"].some(
    (name) => normalizedToolName.value.includes(name),
  ),
);
const filePath = computed(() => {
  if (!isFileRead.value) return "";
  for (const key of ["path", "file_path", "filepath", "target_path", "target"]) {
    const value = props.args?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
});
const fileName = computed(() =>
  filePath.value.split(/[\\/]/).filter(Boolean).at(-1) ?? filePath.value,
);

const toolTitle = computed(() => {
  if (filePath.value) return `读取 ${fileName.value}`;
  const name = normalizedToolName.value;
  if (name.includes("create_file")) return "创建文件";
  if (name.includes("edit_file") || name.includes("write_file")) return "修改文件";
  if (name.includes("list_dir")) return "查看目录";
  if (name.includes("execute") || name.includes("shell")) return "运行命令";
  if (name.includes("search")) return "搜索";
  return props.toolName;
});

const statusLabel = computed(() => {
  if (props.status === "running") return "运行中";
  if (props.status === "error") return "失败";
  return "完成";
});
const isError = computed(() => props.status === "error");
const isRunning = computed(() => props.status === "running");

const fullResult = ref("");
const loadedFilePath = ref("");
const detailLoading = ref(false);
const detailError = ref("");
const loadedBytes = ref(0);
const totalBytes = ref(0);
const displayedResult = computed(() => fullResult.value || props.resultPreview || "");
const progressLabel = computed(() => {
  if (!detailLoading.value || !totalBytes.value) return "";
  return `${Math.min(100, Math.round((loadedBytes.value / totalBytes.value) * 100))}%`;
});

async function loadFullFile() {
  if (!filePath.value || loadedFilePath.value === filePath.value) return;
  detailAbort?.abort();
  const controller = new AbortController();
  detailAbort = controller;
  detailLoading.value = true;
  detailError.value = "";
  fullResult.value = "";
  loadedBytes.value = 0;
  totalBytes.value = 0;
  try {
    const data = await api.workspaceFileTextContent(
      props.runId,
      filePath.value,
      controller.signal,
      (loaded, total) => {
        loadedBytes.value = loaded;
        totalBytes.value = total;
      },
    );
    fullResult.value = data.content;
    loadedFilePath.value = filePath.value;
  } catch (error) {
    if (controller.signal.aborted) return;
    detailError.value = error instanceof Error ? error.message : String(error);
  } finally {
    if (!controller.signal.aborted && detailAbort === controller) detailLoading.value = false;
  }
}

function onToggle(event: Event) {
  const details = event.currentTarget as HTMLDetailsElement;
  if (details.open && filePath.value) void loadFullFile();
}

watch(filePath, () => {
  detailAbort?.abort();
  fullResult.value = "";
  loadedFilePath.value = "";
  detailError.value = "";
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
  detailAbort?.abort();
  if (elapsedTimer !== null) window.clearInterval(elapsedTimer);
});
</script>

<template>
  <details class="tool-call-card" :data-status="status" @toggle="onToggle">
    <summary class="tool-head" title="点击查看工具返回内容">
      <span class="tool-icon"><Terminal :size="13" /></span>
      <span class="tool-action">{{ toolTitle }}</span>
      <span v-if="agentName" class="tool-agent-name">{{ agentName }}</span>
      <span class="tool-status">
        <component :is="statusIcon" :size="12" :class="{ spin: isRunning }" />
        {{ statusLabel }}<span v-if="durationLabel"> · {{ durationLabel }}</span>
      </span>
      <ChevronRight class="tool-chevron" :size="13" />
    </summary>

    <div class="tool-details">
      <section v-if="detailLoading || displayedResult || detailError" class="tool-result" :class="{ error: isError }">
        <header>
          <FileText v-if="filePath" :size="13" />
          <Terminal v-else :size="13" />
          <strong>{{ filePath || "返回结果" }}</strong>
          <span v-if="detailLoading">正在读取完整内容 {{ progressLabel }}</span>
        </header>
        <pre v-if="displayedResult" class="tool-result-text">{{ displayedResult }}</pre>
        <p v-if="detailError" class="tool-detail-error">{{ detailError }}</p>
      </section>
      <p v-else-if="isRunning" class="tool-pending-detail">正在等待工具返回结果。</p>

      <details v-if="argEntries.length" class="tool-args-disclosure">
        <summary>
          <Braces :size="12" />
          参数
          <code>{{ toolName }}</code>
        </summary>
        <div class="tool-args">
          <div v-for="entry in argEntries" :key="entry.key" class="tool-arg">
            <span class="tool-arg-key">{{ entry.key }}</span>
            <pre class="tool-arg-value">{{ entry.value }}</pre>
          </div>
        </div>
      </details>
    </div>
  </details>
</template>
