import { onBeforeUnmount, ref, watch, type Ref } from "vue";
import { api } from "@/lib/api";
import { useRunStream } from "@/composables/useRunStream";
import type {
  AssistantChatMessage,
  ChatMessage,
  EventEnvelope,
  ToolCallChatMessage,
} from "@/types";

/**
 * 对话式 UI 的事件→消息映射层。
 *
 * 把后端 EventEnvelope 流映射成 ChatGPT 式的对话消息序列：
 * - user_message → 用户气泡
 * - assistant_token → 累积进同一个流式助手气泡（按 message_id 归并）
 * - assistant_message → 定稿该助手气泡
 * - tool_call_started / tool_call_result → ToolCallCard（按 tool_call_id 归并）
 * - artifact_created → ArtifactCard
 * - task_started / task_terminated / speaker_selected → 轻量 StatusPill
 *
 * 进入会话时先调 api.listAllEvents 回放历史，再接 SSE 增量。用 event_id 去重
 * 防止历史与 SSE 重叠时重复处理。
 */

interface ThreadIndex {
  // 用具体子类型，避免从联合 ChatMessage 取值时的窄化问题
  assistantByMsgId: Map<string, AssistantChatMessage>;
  toolByCallId: Map<string, ToolCallChatMessage>;
  artifactIds: Set<string>;
  seen: Set<string>;
  // messageId → 最后收到 token 的时间戳，用于超时清除 streaming 光标
  lastTokenAt: Map<string, number>;
}

function newIndex(): ThreadIndex {
  return {
    assistantByMsgId: new Map(),
    toolByCallId: new Map(),
    artifactIds: new Set(),
    seen: new Set(),
    lastTokenAt: new Map(),
  };
}

function payloadOf(env: EventEnvelope): Record<string, any> {
  return (env.payload || {}) as Record<string, any>;
}

function eventKind(eventType: string): string {
  return eventType
    .replace(/^root_graph:/i, "")
    .replace(/[_:\-\s]/g, "")
    .toLowerCase();
}

function agentLabel(env: EventEnvelope, payload: Record<string, any>): string {
  return String(
    payload.agent_name ??
      payload.from_agent_name ??
      payload.agent ??
      env.agent_id ??
      "Agent",
  );
}

/** 把单个事件应用到 messages（就地变更），并更新索引。已见过的 event_id 跳过。 */
function applyOne(messages: ChatMessage[], env: EventEnvelope, idx: ThreadIndex): void {
  if (idx.seen.has(env.event_id)) return;
  idx.seen.add(env.event_id);

  const p = payloadOf(env);

  switch (eventKind(env.event_type)) {
    case "usermessage": {
      messages.push({
        id: env.event_id,
        role: "user",
        content: String(p.content ?? ""),
        createdAt: env.timestamp,
      });
      break;
    }
    case "assistanttoken": {
      const msgId = String(p.message_id ?? "");
      const delta = String(p.delta ?? "");
      // 空 delta 不创建消息（避免空气泡）
      if (!delta) break;
      let m = msgId ? idx.assistantByMsgId.get(msgId) : undefined;
      if (!m) {
        const created: AssistantChatMessage = {
          id: env.event_id,
          role: "assistant",
          content: "",
          streaming: true,
          messageId: msgId || undefined,
          agentId:
            (env.agent_id as string | null) ??
            (p.agent_id as string | null) ??
            null,
          agentName: (p.agent_name as string | null) ?? null,
          createdAt: env.timestamp,
        };
        messages.push(created);
        // Vue wraps objects when they enter a reactive array.  Keep the
        // wrapped instance in the index; mutating ``created`` afterwards
        // bypasses Vue and leaves the UI stuck on the first token until some
        // unrelated render (or a full page refresh) happens.
        m = messages[messages.length - 1] as AssistantChatMessage;
        if (msgId) idx.assistantByMsgId.set(msgId, m);
      }
      m.content += delta;
      // A transient SSE disconnect intentionally clears stale cursors.  If
      // replay or the recovered connection delivers another token for the
      // same message, that token is authoritative evidence that generation
      // resumed, so restore the live state as well as appending the text.
      m.streaming = true;
      // 记录最后收到 token 的时间，用于超时清除 streaming 光标
      if (msgId) idx.lastTokenAt.set(msgId, Date.now());
      break;
    }
    case "assistantmessage": {
      const msgId = String(p.message_id ?? "");
      const content = String(p.content ?? "");
      const existing = msgId ? idx.assistantByMsgId.get(msgId) : undefined;
      if (existing) {
        existing.streaming = false;
        if (content) existing.content = content;
        // 最终内容为空 → 移除空气泡，不残留空消息
        if (!existing.content.trim()) {
          const i = messages.indexOf(existing);
          if (i >= 0) messages.splice(i, 1);
          if (msgId) idx.assistantByMsgId.delete(msgId);
        }
      } else if (content.trim()) {
        // 只有非空内容才创建新消息（避免空气泡）
        const created: AssistantChatMessage = {
          id: env.event_id,
          role: "assistant",
          content,
          streaming: false,
          messageId: msgId || undefined,
          agentId:
            (env.agent_id as string | null) ??
            (p.agent_id as string | null) ??
            null,
          agentName: (p.agent_name as string | null) ?? null,
          createdAt: env.timestamp,
        };
        messages.push(created);
        if (msgId) {
          idx.assistantByMsgId.set(
            msgId,
            messages[messages.length - 1] as AssistantChatMessage,
          );
        }
      }
      break;
    }
    case "toolcallstarted": {
      const tcId = String(p.tool_call_id ?? "");
      const created: ToolCallChatMessage = {
        id: env.event_id,
        type: "tool_call",
        toolCallId: tcId || null,
        toolName: String(p.tool_name ?? "tool"),
        args: (p.arguments as Record<string, unknown>) ?? {},
        status: "running",
        agentName:
          (p.agent_name as string | null) ??
          (env.agent_id as string | null) ??
          null,
        createdAt: env.timestamp,
      };
      messages.push(created);
      if (tcId) {
        idx.toolByCallId.set(
          tcId,
          messages[messages.length - 1] as ToolCallChatMessage,
        );
      }
      break;
    }
    case "toolcallresult": {
      const tcId = String(p.tool_call_id ?? "");
      const existing = tcId ? idx.toolByCallId.get(tcId) : undefined;
      const status: "ok" | "error" = p.status === "error" ? "error" : "ok";
      if (existing) {
        existing.status = status;
        existing.resultPreview = String(p.result_preview ?? "");
        existing.durationMs = (p.duration_ms as number | null) ?? null;
        existing.agentName =
          (p.agent_name as string | null) ??
          existing.agentName ??
          (env.agent_id as string | null) ??
          null;
      } else {
        const created: ToolCallChatMessage = {
          id: env.event_id,
          type: "tool_call",
          toolCallId: tcId || null,
          toolName: String(p.tool_name ?? "tool"),
          args: (p.arguments as Record<string, unknown>) ?? {},
          status,
          resultPreview: String(p.result_preview ?? ""),
          durationMs: (p.duration_ms as number | null) ?? null,
          agentName:
            (p.agent_name as string | null) ??
            (env.agent_id as string | null) ??
            null,
          createdAt: env.timestamp,
        };
        messages.push(created);
        if (tcId) {
          idx.toolByCallId.set(
            tcId,
            messages[messages.length - 1] as ToolCallChatMessage,
          );
        }
      }
      break;
    }
    case "artifactcreated": {
      const artifactId = String(p.artifact_id ?? p.id ?? env.task_id ?? "");
      if (!artifactId || idx.artifactIds.has(artifactId)) break;
      idx.artifactIds.add(artifactId);
      messages.push({
        id: env.event_id,
        type: "artifact",
        artifactId,
        taskId:
          (env.task_id as string | null) ??
          (p.task_id as string | null) ??
          null,
        producedBy:
          (p.produced_by as string | null) ??
          (p.agent as string | null) ??
          (env.agent_id as string | null) ??
          null,
        createdAt: env.timestamp,
      });
      break;
    }
    case "taskstarted": {
      const owner = agentLabel(env, p);
      const taskId = env.task_id ?? p.task_id;
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "running",
        text: p.goal
          ? `开始执行：${String(p.goal).slice(0, 80)}`
          : `${owner} 开始执行${taskId ? ` · ${String(taskId)}` : ""}`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "taskproduced": {
      const artifactIds = Array.isArray(p.artifact_ids)
        ? p.artifact_ids.map(String).filter(Boolean)
        : [];
      if (!artifactIds.length) {
        messages.push({
          id: env.event_id,
          type: "status",
          tone: "running",
          text: `${agentLabel(env, p)} 已提交任务产出，等待验证`,
          createdAt: env.timestamp,
        });
        break;
      }
      for (const artifactId of artifactIds) {
        if (idx.artifactIds.has(artifactId)) continue;
        idx.artifactIds.add(artifactId);
        messages.push({
          id: `${env.event_id}:artifact:${artifactId}`,
          type: "artifact",
          artifactId,
          taskId:
            (env.task_id as string | null) ??
            (p.task_id as string | null) ??
            null,
          producedBy: agentLabel(env, p),
          createdAt: env.timestamp,
        });
      }
      break;
    }
    case "taskcompleted": {
      const taskId = env.task_id ?? p.task_id;
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "ok",
        text: `${agentLabel(env, p)} 的任务已验证通过${taskId ? ` · ${String(taskId)}` : ""}`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "taskfailed": {
      const taskId = env.task_id ?? p.task_id;
      const error = String(p.error ?? p.last_error ?? p.reason ?? "").trim();
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "error",
        text: `${agentLabel(env, p)} 的任务执行失败${taskId ? ` · ${String(taskId)}` : ""}${error ? `：${error}` : ""}`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "taskbudgetexceeded": {
      const taskId = env.task_id ?? p.task_id;
      const used = p.used ?? "?";
      const limit = p.limit ?? "?";
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "error",
        text: `${agentLabel(env, p)} 的任务已达到工具调用上限 ${used}/${limit}${taskId ? ` · ${String(taskId)}` : ""}，已停止继续调用`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "replanrequested": {
      const taskId = env.task_id ?? p.task_id;
      const reason = String(p.reason ?? "").trim();
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "warn",
        text: `${agentLabel(env, p)} 请求重新规划${taskId ? ` · ${String(taskId)}` : ""}${reason ? `：${reason}` : ""}`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "taskstateconflict": {
      const taskId = env.task_id ?? p.task_id;
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "error",
        text: `已阻止过期执行结果${taskId ? ` · ${String(taskId)}` : ""}（${String(p.transition ?? "state transition")}：${String(p.actual_status ?? "unknown")}）`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "artifactverificationcommitfailed": {
      const taskId = env.task_id ?? p.task_id;
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "error",
        text: `产物验证提交失败${taskId ? ` · ${String(taskId)}` : ""}：${String(p.error ?? "unknown error")}`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "permissionrequested": {
      messages.push({
        id: env.event_id,
        type: "approval",
        kind: "permission",
        requestId: String(p.request_id ?? ""),
        operation: String(p.operation ?? p.kind ?? "受治理操作"),
        title: p.title ? String(p.title) : undefined,
        summary: p.summary ? String(p.summary) : undefined,
        reason: p.reason ? String(p.reason) : undefined,
        target: p.target
          ? String(p.target)
          : p.parameters
            ? JSON.stringify(p.parameters)
            : undefined,
        status: String(p.status ?? "pending"),
        createdAt: env.timestamp,
      });
      break;
    }
    case "agentmessage": {
      messages.push({
        id: env.event_id,
        type: "collaboration",
        fromAgent: agentLabel(env, p),
        toAgent: String(
          p.to_agent_name ?? p.to_agent ?? p.to_agent_id ?? "团队",
        ),
        title: p.title ? String(p.title) : null,
        content: String(
          p.content ??
            p.message ??
            p.summary ??
            "Agent 已发送一条协作消息",
        ),
        taskId:
          (env.task_id as string | null) ??
          (p.task_id as string | null) ??
          null,
        createdAt: env.timestamp,
      });
      break;
    }
    case "runcompleted": {
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "ok",
        text: String(p.summary ?? p.message ?? "运行已完成"),
        createdAt: env.timestamp,
      });
      break;
    }
    case "runfailed": {
      const error = String(p.error ?? p.reason ?? p.message ?? "").trim();
      messages.push({
        id: env.event_id,
        type: "status",
        tone: "error",
        text: error ? `运行失败：${error}` : "运行失败",
        createdAt: env.timestamp,
      });
      break;
    }
    case "taskterminated": {
      const status = String(p.status ?? "");
      const tone: "ok" | "warn" | "error" =
        status === "completed" ? "ok" : status === "cancelled" ? "warn" : "error";
      const label =
        status === "completed" ? "完成" : status === "cancelled" ? "已取消" : "失败";
      messages.push({
        id: env.event_id,
        type: "status",
        tone,
        text: `任务${label}（${p.total_rounds ?? 0} 轮，${p.elapsed ?? 0}s）`,
        createdAt: env.timestamp,
      });
      break;
    }
    case "speakerselected": {
      // speaker_selected 是噪声事件（每轮都发射），不进入对话流。
      // 用户关心的是工具调用和助手消息，不是谁在发言。
      break;
    }
    default:
      // actions_emitted / message_published / state_updated / round_started 等不进入对话流，避免噪声
      break;
  }
}

/** 纯函数：把事件序列映射成 ChatMessage 序列（用于历史回放与测试）。 */
export function mapEventsToMessages(events: EventEnvelope[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  const idx = newIndex();
  const sorted = [...events].sort((a, b) => a.sequence - b.sequence);
  for (const env of sorted) applyOne(messages, env, idx);
  return messages;
}

export function useChatThread(runId: Ref<string>) {
  const messages = ref<ChatMessage[]>([]);
  const afterSequence = ref(0);
  const loadingHistory = ref(false);
  const error = ref("");
  const historyReady = ref(false);
  let idx: ThreadIndex = newIndex();
  let loadGeneration = 0;

  function applyEvent(env: EventEnvelope) {
    applyOne(messages.value, env, idx);
    afterSequence.value = Math.max(afterSequence.value, env.sequence);
  }

  async function loadHistory(id: string) {
    const generation = ++loadGeneration;
    historyReady.value = false;
    idx = newIndex();
    messages.value = [];
    afterSequence.value = 0;
    if (!id) {
      loadingHistory.value = false;
      error.value = "";
      return;
    }
    loadingHistory.value = true;
    error.value = "";
    try {
      const events = await api.listAllEvents(id);
      if (generation !== loadGeneration || runId.value !== id) return;
      for (const env of [...events].sort((a, b) => a.sequence - b.sequence)) {
        applyOne(messages.value, env, idx);
      }
      afterSequence.value = events.reduce(
        (maximum, event) => Math.max(maximum, event.sequence),
        0,
      );
    } catch (e) {
      if (generation !== loadGeneration || runId.value !== id) return;
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      if (generation === loadGeneration && runId.value === id) {
        loadingHistory.value = false;
        // Start SSE only after the replay cursor is known.  Starting both in
        // parallel lets a late history response overwrite live token events.
        historyReady.value = true;
      }
    }
  }

  watch(
    runId,
    (id) => {
      void loadHistory(id);
    },
    { immediate: true },
  );

  const stream = useRunStream(runId, afterSequence, applyEvent, historyReady);

  // SSE 断开时清除所有 streaming 光标 — 防止连接中断后光标永久残留。
  watch(() => stream.connected.value, (connected) => {
    if (!connected) {
      for (const msg of idx.assistantByMsgId.values()) {
        if (msg.streaming) msg.streaming = false;
      }
    }
  });

  // Token 超时清除：如果某条 streaming 消息超过 15s 没有收到新 token，
  // 自动将 streaming 置为 false。防止后端未发射 assistant_message 事件
  // （LLM 异常/超时/message_id 不匹配）时光标永久闪烁。
  const cleanupTimer = setInterval(() => {
    const now = Date.now();
    for (const [msgId, msg] of idx.assistantByMsgId) {
      if (msg.streaming) {
        const last = idx.lastTokenAt.get(msgId) ?? 0;
        if (last > 0 && now - last > 8_000) {
          msg.streaming = false;
        }
      }
    }
  }, 5_000);

  onBeforeUnmount(() => {
    clearInterval(cleanupTimer);
  });

  return {
    messages,
    loadingHistory,
    error,
    afterSequence,
    loadHistory,
    ...stream,
  };
}
