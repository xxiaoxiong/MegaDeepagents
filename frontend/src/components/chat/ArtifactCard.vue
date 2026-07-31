<script setup lang="ts">
import { computed } from "vue";
import { FileCode2, FileText, Image, PanelRight } from "@lucide/vue";

const props = defineProps<{
  runId: string;
  artifactId: string;
  path?: string | null;
  artifactType?: string | null;
  sizeBytes?: number | null;
  taskId?: string | null;
  producedBy?: string | null;
}>();

const emit = defineEmits<{ open: [artifactId: string] }>();

// 不再 router.push 跳到任务运行页：点击卡片在聊天页右侧抽屉直接展示产出内容，
// 对齐 codex "聊天 + 右侧文件面板" 的体验。runId 保留为 prop 以备上下文使用。
function open() {
  emit("open", props.artifactId);
}

const fileName = computed(() => {
  const path = props.path?.trim();
  return path ? path.split(/[\\/]/).filter(Boolean).at(-1) ?? path : props.artifactId;
});

const extension = computed(
  () => fileName.value.split(".").pop()?.toLowerCase() ?? "",
);
const icon = computed(() => {
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension.value)) {
    return Image;
  }
  if (["md", "mdx", "txt", "rst"].includes(extension.value)) return FileText;
  return FileCode2;
});

const sizeLabel = computed(() => {
  const bytes = props.sizeBytes;
  if (bytes == null) return "";
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1_024).toFixed(1)} KiB`;
  return `${(bytes / 1_048_576).toFixed(1)} MiB`;
});
</script>

<template>
  <button
    class="artifact-card"
    type="button"
    :title="path ? `打开 ${path}` : `打开产物 ${artifactId}`"
    @click="open"
  >
    <span class="artifact-icon"><component :is="icon" :size="15" /></span>
    <div class="artifact-meta">
      <strong>{{ fileName }}</strong>
      <small>
        <span v-if="artifactType">{{ artifactType }}</span>
        <span v-if="artifactType && sizeLabel"> · </span>
        <span v-if="sizeLabel">{{ sizeLabel }}</span>
        <span v-if="producedBy">{{ artifactType || sizeLabel ? " · " : "" }}{{ producedBy }}</span>
      </small>
    </div>
    <PanelRight :size="14" class="artifact-go" />
  </button>
</template>
