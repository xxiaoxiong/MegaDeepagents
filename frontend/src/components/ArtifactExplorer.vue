<script setup lang="ts">
import { computed, ref, watch } from "vue";
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

const props = defineProps<{
  runId: string;
  artifacts: Artifact[];
  initialArtifactId?: string | null;
}>();
const emit = defineEmits<{ selected: [artifactId: string] }>();

const selected = ref<Artifact | null>(null);
const preview = ref("");
const lineage = ref<Artifact[]>([]);
const truncated = ref(false);
const loading = ref(false);
const query = ref("");
const copied = ref<"content" | "link" | "">("");
let requestGeneration = 0;

const visibleArtifacts = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return [...props.artifacts].reverse();
  return [...props.artifacts]
    .reverse()
    .filter((item) =>
      [
        item.path,
        item.artifact_id,
        item.type,
        item.produced_by,
        item.task_id,
      ].some((value) => String(value ?? "").toLowerCase().includes(needle)),
    );
});

const extension = computed(
  () => selected.value?.path.split(".").pop()?.toLowerCase() ?? "",
);
const isMarkdown = computed(() =>
  ["md", "mdx", "markdown"].includes(extension.value),
);
const isImage = computed(() =>
  ["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension.value),
);

async function open(item: Artifact) {
  selected.value = item;
  preview.value = "";
  lineage.value = [];
  truncated.value = false;
  loading.value = true;
  emit("selected", item.artifact_id);
  const generation = ++requestGeneration;
  try {
    const lineageRequest = api
      .artifactLineage(props.runId, item.artifact_id)
      .catch(() => []);
    if (isImage.value) {
      const history = await lineageRequest;
      if (generation !== requestGeneration) return;
      lineage.value = history;
      return;
    }
    const [data, history] = await Promise.all([
      api.artifactContent(props.runId, item.artifact_id),
      lineageRequest,
    ]);
    if (generation !== requestGeneration) return;
    preview.value = data.content;
    truncated.value = data.truncated;
    lineage.value = history;
  } catch (error) {
    if (generation !== requestGeneration) return;
    preview.value = error instanceof Error ? error.message : String(error);
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
    if (target && target.artifact_id !== selected.value?.artifact_id) {
      void open(target);
    } else if (target) {
      selected.value = target;
    }
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
</script>

<template>
  <div class="artifact-explorer">
    <EmptyState
      v-if="!artifacts.length"
      title="尚无 Artifact"
      description="Worker 产出的文件与验证证据会在此形成可追溯版本链。"
    />
    <div v-else class="artifact-grid">
      <aside class="artifact-list-pane">
        <label class="artifact-search">
          <Search :size="14" />
          <input
            v-model="query"
            type="search"
            placeholder="搜索文件、Agent 或 Task"
          />
        </label>
        <div class="artifact-list">
          <button
            v-for="item in visibleArtifacts"
            :key="item.artifact_id"
            :class="{ active: selected?.artifact_id === item.artifact_id }"
            @click="open(item)"
          >
            <span class="artifact-file-icon">
              <Image v-if="['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(item.path.split('.').pop()?.toLowerCase() ?? '')" :size="16" />
              <FileText v-else-if="['md', 'mdx', 'txt'].includes(item.path.split('.').pop()?.toLowerCase() ?? '')" :size="16" />
              <FileCode2 v-else :size="16" />
            </span>
            <span>
              <strong>{{ item.path }}</strong>
              <small>{{ item.type }} · {{ size(item.size_bytes) }}</small>
              <em>{{ item.produced_by || "unknown agent" }} · {{ item.task_id }}</em>
            </span>
            <span class="artifact-version">v{{ item.version }}</span>
          </button>
        </div>
        <p v-if="!visibleArtifacts.length" class="artifact-no-result">
          没有匹配的交付物。
        </p>
      </aside>

      <section class="artifact-preview">
        <header v-if="selected">
          <div>
            <span class="eyebrow">{{ selected.artifact_id }}</span>
            <strong>{{ selected.path }}</strong>
            <small>
              {{ selected.type }} · {{ size(selected.size_bytes) }} · SHA-256
              {{ selected.content_hash.slice(0, 10) }}
            </small>
          </div>
          <div class="artifact-actions">
            <button class="btn btn-ghost btn-small" @click="copy('link')">
              <Check v-if="copied === 'link'" :size="13" />
              <ExternalLink v-else :size="13" />
              {{ copied === "link" ? "已复制" : "复制链接" }}
            </button>
            <button
              v-if="!isImage"
              class="btn btn-ghost btn-small"
              :disabled="!preview"
              @click="copy('content')"
            >
              <Check v-if="copied === 'content'" :size="13" />
              <Copy v-else :size="13" />
              {{ copied === "content" ? "已复制" : "复制内容" }}
            </button>
            <a
              class="btn btn-secondary btn-small"
              :href="downloadUrl(selected)"
            >
              <Download :size="13" /> 下载
            </a>
          </div>
        </header>

        <div v-if="selected && lineage.length > 1" class="artifact-lineage">
          <GitCommitHorizontal :size="14" />
          <span>版本链</span>
          <button
            v-for="version in lineage"
            :key="version.artifact_id"
            :class="{ active: version.artifact_id === selected.artifact_id }"
            @click="open(version)"
          >
            v{{ version.version }}
          </button>
        </div>

        <div v-if="loading" class="preview-loading">
          <LoaderCircle class="spin" :size="20" /> 读取 Artifact…
        </div>
        <img
          v-else-if="selected && isImage"
          class="artifact-image-preview"
          :src="downloadUrl(selected)"
          :alt="selected.path"
        />
        <div
          v-else-if="selected && isMarkdown"
          class="artifact-markdown-preview"
        >
          <MarkdownMessage :content="preview" />
        </div>
        <pre v-else-if="selected"><code>{{ preview }}</code></pre>
        <div v-else class="preview-placeholder">
          选择一个 Artifact 查看内容
        </div>
        <small v-if="truncated" class="truncate-note">
          内容较大，当前仅展示前 512 KiB；下载可获取完整文件。
        </small>
      </section>
    </div>
  </div>
</template>
