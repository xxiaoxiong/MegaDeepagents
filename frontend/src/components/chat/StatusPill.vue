<script setup lang="ts">
import { computed } from "vue";
import { Check, Info, LoaderCircle, TriangleAlert, X } from "@lucide/vue";

const props = defineProps<{
  tone: "info" | "running" | "ok" | "error" | "warn";
  text: string;
}>();

const icon = computed(() => {
  switch (props.tone) {
    case "info":
      return Info;
    case "running":
      return LoaderCircle;
    case "ok":
      return Check;
    case "warn":
      return TriangleAlert;
    case "error":
      return X;
  }
});
</script>

<template>
  <div class="status-pill" :data-tone="tone">
    <component
      :is="icon"
      :size="13"
      :class="{ spin: tone === 'running' }"
    />
    <span>{{ text }}</span>
  </div>
</template>
