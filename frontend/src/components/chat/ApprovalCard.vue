<script setup lang="ts">
import { ref } from "vue";
import { Check, FileCheck2, KeyRound, X } from "@lucide/vue";
import { api } from "@/lib/api";

const props = defineProps<{
  runId: string;
  kind: "permission" | "plan";
  requestId: string;
  operation?: string;
  title?: string;
  summary?: string;
  reason?: string;
  target?: string;
  status: string;
}>();

const emit = defineEmits<{ decided: [] }>();
const feedback = ref("");
const working = ref(false);
const decided = ref(props.status !== "pending");

async function permission(decision: "approve_once" | "approve_run" | "deny") {
  working.value = true;
  try {
    await api.decidePermission(
      props.runId,
      props.requestId,
      decision,
      feedback.value,
    );
    decided.value = true;
    emit("decided");
  } finally {
    working.value = false;
  }
}

async function plan(approved: boolean) {
  working.value = true;
  try {
    await api.decidePlan(
      props.runId,
      props.requestId,
      approved,
      feedback.value,
    );
    decided.value = true;
    emit("decided");
  } finally {
    working.value = false;
  }
}
</script>

<template>
  <div class="approval-card" :data-kind="kind" :data-decided="decided">
    <header>
      <span class="approval-icon">
        <KeyRound v-if="kind === 'permission'" :size="15" />
        <FileCheck2 v-else :size="15" />
      </span>
      <div>
        <span class="eyebrow">
          {{ kind === "permission" ? "权限请求" : "计划审批" }}
        </span>
        <h4>{{ title || operation || "Agent 请求审批" }}</h4>
      </div>
    </header>
    <p v-if="summary || reason">{{ summary || reason }}</p>
    <code v-if="target" class="approval-target">{{ target }}</code>
    <textarea
      v-if="!decided"
      v-model="feedback"
      rows="2"
      placeholder="说明（可选）"
    />
    <footer v-if="!decided">
      <button
        class="btn btn-ghost btn-small danger"
        :disabled="working"
        type="button"
        @click="kind === 'permission' ? permission('deny') : plan(false)"
      >
        <X :size="13" /> {{ kind === "permission" ? "拒绝" : "退回" }}
      </button>
      <button
        v-if="kind === 'permission'"
        class="btn btn-secondary btn-small"
        type="button"
        :disabled="working"
        @click="permission('approve_once')"
      >
        <Check :size="13" /> 仅本次
      </button>
      <button
        class="btn btn-primary btn-small"
        type="button"
        :disabled="working"
        @click="kind === 'permission' ? permission('approve_run') : plan(true)"
      >
        <Check :size="13" /> {{ kind === "permission" ? "本次运行" : "批准" }}
      </button>
    </footer>
    <footer v-else class="approval-done">已处理 · {{ status }}</footer>
  </div>
</template>
