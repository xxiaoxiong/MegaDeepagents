<script setup lang="ts">
import { ArrowRight, GitFork } from "@lucide/vue";

defineProps<{
  fromAgent?: string | null;
  toAgent?: string | null;
  title?: string | null;
  content: string;
  taskId?: string | null;
}>();
</script>

<template>
  <article class="collaboration-card">
    <span class="collaboration-icon"><GitFork :size="14" /></span>
    <span class="collaboration-copy">
      <span class="collaboration-route">
        <strong>{{ fromAgent || "Agent" }}</strong>
        <ArrowRight :size="11" />
        <strong>{{ toAgent || "团队" }}</strong>
        <small v-if="taskId">Task · {{ taskId }}</small>
      </span>
      <span v-if="title" class="collaboration-title">{{ title }}</span>
      <span class="collaboration-content">{{ content }}</span>
    </span>
  </article>
</template>
