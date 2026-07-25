<script setup lang="ts">
import { ref } from "vue";
import { Bot, MessageSquare, Send, Square } from "@lucide/vue";
import { api } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
import type { Agent } from "@/types";

const props = defineProps<{ runId: string; agents: Agent[] }>();
const emit = defineEmits<{ refresh: [] }>();
const selected = ref("");
const content = ref("");
const working = ref(false);

async function send() {
  if (!selected.value || !content.value.trim()) return;
  working.value = true;
  try {
    await api.sendAgentMessage(props.runId, selected.value, content.value.trim());
    content.value = "";
  } finally {
    working.value = false;
  }
}

async function stop(agentId: string) {
  working.value = true;
  try {
    await api.stopAgent(props.runId, agentId);
    emit("refresh");
  } finally {
    working.value = false;
  }
}
</script>

<template>
  <section class="panel agent-panel">
    <header class="panel-heading">
      <div>
        <span class="eyebrow">Teammates</span>
        <h2>Agent 团队</h2>
      </div>
      <span class="count-mark">{{ agents.length }}</span>
    </header>
    <EmptyState
      v-if="!agents.length"
      title="正在组建团队"
      description="Agent 创建后会显示身份、状态与当前任务。"
    />
    <div v-else class="agent-list">
      <button
        v-for="agent in agents"
        :key="agent.agent_id"
        class="agent-row"
        :class="{ selected: selected === agent.agent_id }"
        @click="selected = agent.agent_id"
      >
        <span class="agent-avatar"><Bot :size="18" /></span>
        <span class="agent-copy">
          <strong>{{ agent.name || agent.role }}</strong>
          <small>{{ agent.role }} · {{ agent.current_task_id || "待命" }}</small>
        </span>
        <StatusBadge :status="agent.status" />
      </button>
    </div>
    <div v-if="selected" class="agent-message-box">
      <div class="message-label">
        <MessageSquare :size="15" />
        向所选 Agent 注入上下文
      </div>
      <textarea
        v-model="content"
        rows="3"
        placeholder="补充约束、验收标准或问题线索…"
        @keydown.ctrl.enter="send"
      />
      <div class="message-actions">
        <button class="btn btn-ghost danger" :disabled="working" @click="stop(selected)">
          <Square :size="14" /> 停止 Agent
        </button>
        <button class="btn btn-primary" :disabled="working || !content.trim()" @click="send">
          <Send :size="14" /> 发送
        </button>
      </div>
    </div>
  </section>
</template>
