<script setup lang="ts">
import { computed } from "vue";
import { renderMarkdown } from "@/composables/useMarkdown";

const props = defineProps<{
  content: string;
  streaming?: boolean;
}>();

const html = computed(() => renderMarkdown(props.content));
</script>

<template>
  <div class="markdown-message">
    <div class="markdown-body" v-html="html" />
    <span v-if="streaming" class="stream-cursor" aria-label="正在生成" />
  </div>
</template>
