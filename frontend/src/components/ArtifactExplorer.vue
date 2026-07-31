<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  Check,
  Copy,
  Download,
  ExternalLink,
  FileCode2,
  FileText,
  GitCommitHorizontal,
  Image,
  LoaderCircle,
  Search,
} from "@lucide/vue";
import { api } from "@/lib/api";
import EmptyState from "@/components/EmptyState.vue";
import MarkdownMessage from "@/components/chat/MarkdownMessage.vue";
import type { Artifact } from "@/types";

const props = withDefaults(
  defineProps<{
    runId: string;
    artifacts: Artifact[];
    initialArtifactId?: string | null;
    compact?: boolean;
  }>(),
  { compact: false },
);
const emit = defineEmits<{ selected: [artifactId: string] }>();

const selected = ref<Artifact | null>(null);
const preview = ref("");
const previewError = ref("");
const previewLoadedBytes = ref(0);
const previewTotalBytes = ref(0);
const lineage = ref<Artifact[]>([]);
const loading = ref(false);
const query = ref("");
const copied = ref<"content" | "link" | "">("");
let requestGeneration = 0;
let previewAbort: AbortController | null = null;

const visibleArtifacts = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return [...props.artifacts].reverse();
  return [...props.artifacts]
    .reverse()
    .filter((item) =>
      [item.path, item.artifact_id, item.type, item.produced_by, item.task_id].some(
        (value) => String(value ?? "").toLowerCase().includes(needle),
      ),
    );
});

const extension = computed(
  () => selected.value?.path.split(".").pop()?.toLowerCase() ?? "",
);
const isMarkdown = computed(() => ["md", "mdx", "markdown"].includes(extension.value));
const isImage = computed(() =>
  ["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension.value),
);
const loadingLabel = computed(() => {
  if (!previewTotalBytes.value) return "读取文件…";
  const percent = Math.min(
    100,
    Math.round((previewLoadedBytes.value / previewTotalBytes.value) * 100),
  );
  return `读取完整文件 ${percent}%`;
});

async function open(item: Artifact) {
  selected.value = item;
  preview.value = "";
  previewError.value = "";
  previewLoadedBytes.value = 0;
  previewTotalBytes.value = item.size_bytes;
  lineage.value = [];
  loading.value = true;
  emit("selected", item.artifact_id);
  const generation = ++requestGeneration;
  previewAbort?.abort();
  const controller = new AbortController();
  previewAbort = controller;
  try {
    const lineageRequest = api.artifactLineage(props.runId, item.artifact_id).catch(() => []);
    if (isImage.value) {
      const history = await lineageRequest;
      if (generation !== requestGeneration) return;
      lineage.value = history;
      return;
    }
    const [data, history] = await Promise.all([
      api.artifactTextContent(
        props.runId,
        item.artifact_id,
        controller.signal,
        (loaded, total) => {
          if (generation !== requestGeneration) return;
          previewLoadedBytes.value = loaded;
          previewTotalBytes.value = total;
        },
      ),
      lineageRequest,
    ]);
    if (generation !== requestGeneration) return;
    preview.value = data.content;
    lineage.value = history;
  } catch (error) {
    if (generation !== requestGeneration || controller.signal.aborted) return;
    previewError.value = error instanceof Error ? error.message : String(error);
  } finally {
    if (generation === requestGeneration) loading.value = false;
  }
}

watch(
  [() => props.initialArtifactId, () => props.artifacts],
  ([artifactId, artifacts]) => {
    if (!artifacts.length) {
      selected.value = null;
      return;
    }
    const target =
      artifacts.find((item) => item.artifact_id === artifactId) ??
      artifacts.find((item) => item.artifact_id === selected.value?.artifact_id) ??
      artifacts.at(-1) ??
      null;
    if (target && target.artifact_id !== selected.value?.artifact_id) void open(target);
    else if (target) selected.value = target;
  },
  { immediate: true },
);

function downloadUrl(item: Artifact) {
  return `${api.baseUrl()}/api/v1/runs/${props.runId}/artifacts/${item.artifact_id}/download`;
}

async function copy(kind: "content" | "link") {
  if (!selected.value) return;
  const value =
    kind === "content"
      ? preview.value
      : `${window.location.origin}${window.location.pathname}#artifact-${encodeURIComponent(selected.value.artifact_id)}`;
  await navigator.clipboard.writeText(value);
  copied.value = kind;
  window.setTimeout(() => {
    if (copied.value === kind) copied.value = "";
  }, 1_500);
}

const size = (bytes: number) => {
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1_024).toFixed(1)} KiB`;
  return `${(bytes / 1_048_576).toFixed(1)} MiB`;
};

const fileName = (path: string) => path.split(/[\\/]/).filter(Boolean).at(-1) || path;

onBeforeUnmount(() => previewAbort?.abort());
</script>

<template>
  <div class="artifact-explorer" :class="{ compact }">
    <EmptyState
      v-if="!artifacts.length"
      title="尚无产出文件"
      description="Agent 生成的文件会在这里直接打开。"
    />
    <div v-else class="artifact-grid">
      <aside class="artifact-list-pane">
        <label class="artifact-search">
          <Search :size="14" />
          <input v-model="query" type="search" placeholder="搜索文件" />
        </label>
        <div class="artifact-list" role="listbox" aria-label="产出文件">
          <button
            v-for="item in visibleArtifacts"
            :key="item.artifact_id"
            :class="{ active: selected?.artifact_id === item.artifact_id }"
            :title="item.path"
            role="option"
            :aria-selected="selected?.artifact_id === item.artifact_id"
            @click="open(item)"
          >
            <span class="artifact-file-icon">
              <Image
                v-if="['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(item.path.split('.').pop()?.toLowerCase() ?? '')"
                :size="16"
              />
              <FileText
                v-else-if="['md', 'mdx', 'txt'].includes(item.path.split('.').pop()?.toLowerCase() ?? '')"
                :size="16"
              />
              <FileCode2 v-else :size="16" />
            </span>
            <span>
              <strong>{{ fileName(item.path) }}</strong>
              <small>{{ item.type }} · {{ size(item.size_bytes) }}</small>
              <em v-if="!compact">{{ item.produced_by || "unknown agent" }} · {{ item.task_id }}</em>
            </span>
            <span class="artifact-version">v{{ item.version }}</span>
          </button>
        </div>
        <p v-if="!visibleArtifacts.length" class="artifact-no-result">没有匹配的文件。</p>
      </aside>

      <section class="artifact-preview">
        <header v-if="selected">
          <div class="artifact-preview-title">
            <strong>{{ selected.path }}</strong>
            <small>
              {{ selected.type }} · {{ size(selected.size_bytes) }} · v{{ selected.version }}
              <span v-if="!compact"> · {{ selected.content_hash.slice(0, 10) }}</span>
            </small>
          </div>
          <div class="artifact-actions">
            <button class="btn btn-ghost btn-small" @click="copy('link')">
              <Check v-if="copied === 'link'" :size="13" />
              <ExternalLink v-else :size="13" />
              {{ copied === "link" ? "已复制" : "链接" }}
            </button>
            <button
              v-if="!isImage"
              class="btn btn-ghost btn-small"
              :disabled="!preview"
              @click="copy('content')"
            >
              <Check v-if="copied === 'content'" :size="13" />
              <Copy v-else :size="13" />
              {{ copied === "content" ? "已复制" : "复制" }}
            </button>
            <a class="btn btn-secondary btn-small" :href="downloadUrl(selected)">
              <Download :size="13" /> 下载
            </a>
          </div>
        </header>

        <div v-if="selected && lineage.length > 1" class="artifact-lineage">
          <GitCommitHorizontal :size="14" />
          <span>版本</span>
          <button
            v-for="version in lineage"
            :key="version.artifact_id"
            :class="{ active: version.artifact_id === selected.artifact_id }"
            @click="open(version)"
          >
            v{{ version.version }}
          </button>
        </div>

        <div class="artifact-preview-content">
          <div v-if="loading" class="preview-loading">
            <LoaderCircle class="spin" :size="18" /> {{ loadingLabel }}
          </div>
          <div v-else-if="previewError" class="preview-error">{{ previewError }}</div>
          <img
            v-else-if="selected && isImage"
            class="artifact-image-preview"
            :src="downloadUrl(selected)"
            :alt="selected.path"
          />
          <div v-else-if="selected && isMarkdown" class="artifact-markdown-preview">
            <MarkdownMessage :content="preview" />
          </div>
          <pre v-else-if="selected"><code>{{ preview }}</code></pre>
          <div v-else class="preview-placeholder">选择一个文件查看完整内容</div>
        </div>
      </section>
    </div>
  </div>
</template>
