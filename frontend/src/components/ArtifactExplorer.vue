<script setup lang="ts">
import { ref } from "vue";
import { Download, FileCode2, LoaderCircle } from "@lucide/vue";
import { api } from "@/lib/api";
import EmptyState from "@/components/EmptyState.vue";
import type { Artifact } from "@/types";

const props = defineProps<{ runId: string; artifacts: Artifact[] }>();
const selected = ref<Artifact | null>(null);
const preview = ref("");
const truncated = ref(false);
const loading = ref(false);

async function open(item: Artifact) {
  selected.value = item;
  preview.value = "";
  loading.value = true;
  try {
    const data = await api.artifactContent(props.runId, item.artifact_id);
    preview.value = data.content;
    truncated.value = data.truncated;
  } catch (error) {
    preview.value = error instanceof Error ? error.message : String(error);
  } finally {
    loading.value = false;
  }
}

function downloadUrl(item: Artifact) {
  return `${api.baseUrl()}/api/v1/runs/${props.runId}/artifacts/${item.artifact_id}/download`;
}
</script>

<template>
  <div class="artifact-explorer">
    <EmptyState
      v-if="!artifacts.length"
      title="尚无 Artifact"
      description="Worker 产出的文件与验证证据会在此形成可追溯版本链。"
    />
    <div v-else class="artifact-grid">
      <div class="artifact-list">
        <button
          v-for="item in artifacts"
          :key="item.artifact_id"
          :class="{ active: selected?.artifact_id === item.artifact_id }"
          @click="open(item)"
        >
          <FileCode2 :size="17" />
          <span>
            <strong>{{ item.path }}</strong>
            <small>v{{ item.version }} · {{ item.type }} · {{ item.produced_by }}</small>
          </span>
        </button>
      </div>
      <div class="artifact-preview">
        <header v-if="selected">
          <div>
            <span class="eyebrow">{{ selected.artifact_id }}</span>
            <strong>{{ selected.path }}</strong>
          </div>
          <a class="btn btn-secondary" :href="downloadUrl(selected)">
            <Download :size="14" /> 下载
          </a>
        </header>
        <div v-if="loading" class="preview-loading">
          <LoaderCircle class="spin" :size="20" /> 读取 Artifact…
        </div>
        <pre v-else-if="selected"><code>{{ preview }}</code></pre>
        <div v-else class="preview-placeholder">选择一个 Artifact 查看内容</div>
        <small v-if="truncated" class="truncate-note">内容较大，当前仅展示前 512 KiB。</small>
      </div>
    </div>
  </div>
</template>
