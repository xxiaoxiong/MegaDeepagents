<script setup lang="ts">
import { computed } from "vue";
import type {
  ApprovalChatMessage,
  ArtifactChatMessage,
  AssistantChatMessage,
  ChatMessage,
  StatusChatMessage,
  ToolCallChatMessage,
  UserChatMessage,
} from "@/types";
import MarkdownMessage from "./MarkdownMessage.vue";
import ToolCallCard from "./ToolCallCard.vue";
import ArtifactCard from "./ArtifactCard.vue";
import ApprovalCard from "./ApprovalCard.vue";
import StatusPill from "./StatusPill.vue";

const props = defineProps<{
  message: ChatMessage;
  runId: string;
}>();

// ChatMessage 联合有两个判别字段（user/assistant 用 role，其余用 type），
// 模板里直接 v-if="message.type" 会被 vue-tsc 拒绝（联合窄化），改用类型守卫 computed。
function isUser(m: ChatMessage): m is UserChatMessage {
  return "role" in m && m.role === "user";
}
function isAssistant(m: ChatMessage): m is AssistantChatMessage {
  return "role" in m && m.role === "assistant";
}
function isToolCall(m: ChatMessage): m is ToolCallChatMessage {
  return "type" in m && m.type === "tool_call";
}
function isArtifact(m: ChatMessage): m is ArtifactChatMessage {
  return "type" in m && m.type === "artifact";
}
function isApproval(m: ChatMessage): m is ApprovalChatMessage {
  return "type" in m && m.type === "approval";
}
function isStatus(m: ChatMessage): m is StatusChatMessage {
  return "type" in m && m.type === "status";
}

const userMsg = computed(() =>
  isUser(props.message) ? props.message : null,
);
const assistantMsg = computed(() =>
  isAssistant(props.message) ? props.message : null,
);
const toolCallMsg = computed(() =>
  isToolCall(props.message) ? props.message : null,
);
const artifactMsg = computed(() =>
  isArtifact(props.message) ? props.message : null,
);
const approvalMsg = computed(() =>
  isApproval(props.message) ? props.message : null,
);
const statusMsg = computed(() =>
  isStatus(props.message) ? props.message : null,
);
</script>

<template>
  <!-- 用户气泡：右对齐 -->
  <div v-if="userMsg" class="msg-row user">
    <div class="msg-bubble user-bubble">
      <MarkdownMessage :content="userMsg.content" />
    </div>
  </div>

  <!-- 助手气泡：左对齐 + 头像 + 流式光标 -->
  <div v-else-if="assistantMsg" class="msg-row assistant">
    <div class="msg-avatar" :title="assistantMsg.agentName ?? 'Agent'">
      {{ (assistantMsg.agentName ?? "A").slice(0, 1).toUpperCase() }}
    </div>
    <div class="msg-bubble assistant-bubble">
      <div v-if="assistantMsg.agentName" class="msg-sender">
        {{ assistantMsg.agentName }}
      </div>
      <MarkdownMessage
        :content="assistantMsg.content"
        :streaming="assistantMsg.streaming"
      />
    </div>
  </div>

  <!-- 工具调用卡片：占满宽度 -->
  <div v-else-if="toolCallMsg" class="msg-row inline">
    <ToolCallCard
      :tool-name="toolCallMsg.toolName"
      :args="toolCallMsg.args"
      :status="toolCallMsg.status"
      :result-preview="toolCallMsg.resultPreview"
      :duration-ms="toolCallMsg.durationMs"
      :agent-name="toolCallMsg.agentName"
    />
  </div>

  <!-- 产出 Artifact 卡片 -->
  <div v-else-if="artifactMsg" class="msg-row inline">
    <ArtifactCard
      :run-id="runId"
      :artifact-id="artifactMsg.artifactId"
      :task-id="artifactMsg.taskId"
      :produced-by="artifactMsg.producedBy"
    />
  </div>

  <!-- 审批卡片 -->
  <div v-else-if="approvalMsg" class="msg-row inline">
    <ApprovalCard
      :run-id="runId"
      :kind="approvalMsg.kind"
      :request-id="approvalMsg.requestId"
      :operation="approvalMsg.operation"
      :title="approvalMsg.title"
      :summary="approvalMsg.summary"
      :reason="approvalMsg.reason"
      :target="approvalMsg.target"
      :status="approvalMsg.status"
    />
  </div>

  <!-- 状态药丸 -->
  <div v-else-if="statusMsg" class="msg-row inline">
    <StatusPill :tone="statusMsg.tone" :text="statusMsg.text" />
  </div>
</template>
