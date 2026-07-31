<script setup lang="ts">
import { FileCode2, PanelRight } from "@lucide/vue";

const props = defineProps<{
  runId: string;
  artifactId: string;
  taskId?: string | null;
  producedBy?: string | null;
}>();

const emit = defineEmits<{ open: [artifactId: string] }>();

// 不再 router.push 跳到任务运行页：点击卡片在聊天页右侧抽屉直接展示产出内容，
// 对齐 codex "聊天 + 右侧文件面板" 的体验。runId 保留为 prop 以备上下文使用。
function open() {
  emit("open", props.artifactId);
}
</script>

<template>
  <button class="artifact-card" type="button" @click="open">
    <span class="artifact-icon"><FileCode2 :size="16" /></span>
    <div class="artifact-meta">
      <strong>产出 Artifact</strong>
      <code>{{ artifactId }}</code>
      <small v-if="producedBy">由 {{ producedBy }} 生成</small>
    </div>
    <PanelRight :size="14" class="artifact-go" />
  </button>
</template>
