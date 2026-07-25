<script setup lang="ts">
import { ref } from "vue";
import { Check, FileCheck2, KeyRound, X } from "@lucide/vue";
import { api } from "@/lib/api";
import EmptyState from "@/components/EmptyState.vue";
import type { PermissionRequest, PlanRequest } from "@/types";

const props = defineProps<{
  runId: string;
  permissions: PermissionRequest[];
  plans: PlanRequest[];
}>();
const emit = defineEmits<{ refresh: [] }>();
const feedback = ref<Record<string, string>>({});
const working = ref("");

async function permission(
  requestId: string,
  decision: "approve_once" | "approve_run" | "deny",
) {
  working.value = requestId;
  try {
    await api.decidePermission(
      props.runId,
      requestId,
      decision,
      feedback.value[requestId] ?? "",
    );
    emit("refresh");
  } finally {
    working.value = "";
  }
}

async function plan(planId: string, approved: boolean) {
  working.value = planId;
  try {
    await api.decidePlan(
      props.runId,
      planId,
      approved,
      feedback.value[planId] ?? "",
    );
    emit("refresh");
  } finally {
    working.value = "";
  }
}
</script>

<template>
  <div class="approval-queue">
    <EmptyState
      v-if="!permissions.length && !plans.length"
      title="没有待审批项目"
      description="高风险工具调用与 Teammate 计划会在此等待你的决定。"
    />
    <article v-for="item in permissions" :key="item.request_id" class="approval-card">
      <header>
        <span class="approval-icon"><KeyRound :size="17" /></span>
        <div>
          <span class="eyebrow">权限请求</span>
          <h3>{{ item.operation }}</h3>
        </div>
      </header>
      <p>{{ item.reason || "Agent 请求执行受治理操作。" }}</p>
      <code v-if="item.target">{{ item.target }}</code>
      <textarea
        v-model="feedback[item.request_id]"
        rows="2"
        placeholder="决定说明（可选）"
      />
      <footer>
        <button class="btn btn-ghost danger" @click="permission(item.request_id, 'deny')">
          <X :size="14" /> 拒绝
        </button>
        <button class="btn btn-secondary" @click="permission(item.request_id, 'approve_once')">
          <Check :size="14" /> 仅本次
        </button>
        <button class="btn btn-primary" @click="permission(item.request_id, 'approve_run')">
          <Check :size="14" /> 本次运行
        </button>
      </footer>
    </article>

    <article v-for="item in plans" :key="item.plan_id" class="approval-card">
      <header>
        <span class="approval-icon plan"><FileCheck2 :size="17" /></span>
        <div>
          <span class="eyebrow">计划审批</span>
          <h3>{{ item.title || "Agent 执行计划" }}</h3>
        </div>
      </header>
      <p>{{ item.summary || item.reason || "请审阅 Agent 的执行计划。" }}</p>
      <textarea
        v-model="feedback[item.plan_id]"
        rows="2"
        placeholder="修改建议或拒绝原因（可选）"
      />
      <footer>
        <button class="btn btn-ghost danger" @click="plan(item.plan_id, false)">
          <X :size="14" /> 退回
        </button>
        <button class="btn btn-primary" @click="plan(item.plan_id, true)">
          <Check :size="14" /> 批准计划
        </button>
      </footer>
    </article>
  </div>
</template>
