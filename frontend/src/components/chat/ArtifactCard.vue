<script setup lang="ts">
import { ExternalLink, FileCode2 } from "@lucide/vue";
import { useRouter } from "vue-router";

const props = defineProps<{
  runId: string;
  artifactId: string;
  taskId?: string | null;
  producedBy?: string | null;
}>();

const router = useRouter();
function open() {
  router.push({
    path: `/runs/${props.runId}`,
    hash: `#artifact-${props.artifactId}`,
  });
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
    <ExternalLink :size="14" class="artifact-go" />
  </button>
</template>
