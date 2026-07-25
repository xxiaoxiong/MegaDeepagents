<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ status: string }>();
const labelMap: Record<string, string> = {
  created: "已创建",
  running: "运行中",
  paused: "已暂停",
  waiting_human: "等待审批",
  succeeded: "已完成",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  pending: "待处理",
  claimed: "已认领",
  produced: "已产出",
  verifying: "验证中",
  repair_required: "等待修复",
  replan_required: "等待重排",
  blocked: "已阻塞",
  idle: "空闲",
  stopped: "已停止",
};
const state = computed(() => {
  if (["succeeded", "completed", "verified"].includes(props.status)) return "ok";
  if (["failed", "cancelled", "rejected"].includes(props.status)) return "bad";
  if (["running", "claimed", "verifying", "planning"].includes(props.status))
    return "active";
  if (
    ["waiting_human", "repair_required", "replan_required", "blocked"].includes(
      props.status,
    )
  )
    return "warn";
  return "quiet";
});
</script>

<template>
  <span class="status-badge" :data-state="state">
    <i />
    {{ labelMap[status] ?? status }}
  </span>
</template>
