<script setup lang="ts">
import { computed } from "vue";
import { Activity, Radio } from "@lucide/vue";
import EmptyState from "@/components/EmptyState.vue";
import type { EventEnvelope } from "@/types";

const props = defineProps<{ events: EventEnvelope[]; connected?: boolean }>();
const recent = computed(() => [...props.events].reverse().slice(0, 150));

const prettyType = (value: string) =>
  value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const time = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
</script>

<template>
  <section class="event-timeline">
    <div class="timeline-head">
      <span>
        <Activity :size="16" />
        审计事件
      </span>
      <span class="live-state" :data-connected="connected">
        <Radio :size="13" />
        {{ connected ? "实时连接" : "等待重连" }}
      </span>
    </div>
    <EmptyState v-if="!recent.length" title="暂无事件" />
    <ol v-else>
      <li v-for="event in recent" :key="event.event_id">
        <span class="event-sequence">#{{ event.sequence }}</span>
        <div>
          <strong>{{ prettyType(event.event_type) }}</strong>
          <small>
            {{ time(event.timestamp) }}
            <template v-if="event.task_id"> · {{ event.task_id }}</template>
          </small>
        </div>
      </li>
    </ol>
  </section>
</template>
