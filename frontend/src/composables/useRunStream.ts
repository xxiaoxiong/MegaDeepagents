import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { api } from "@/lib/api";
import type { EventEnvelope } from "@/types";

export function useRunStream(
  runId: Ref<string>,
  afterSequence: Ref<number>,
  onEvent: (event: EventEnvelope) => void,
) {
  const connected = ref(false);
  const reconnecting = ref(false);
  const reconnectAttempt = ref(0);
  const lastMessageAt = ref<string | null>(null);
  const lastError = ref("");
  let source: EventSource | null = null;
  let retryTimer: number | null = null;
  let stopped = false;

  const disconnect = () => {
    source?.close();
    source = null;
    connected.value = false;
    if (retryTimer !== null) window.clearTimeout(retryTimer);
    retryTimer = null;
  };

  const scheduleReconnect = () => {
    if (stopped || retryTimer !== null) return;
    reconnecting.value = true;
    reconnectAttempt.value += 1;
    const delay = Math.min(
      30_000,
      1_000 * 2 ** Math.min(reconnectAttempt.value - 1, 5),
    );
    retryTimer = window.setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  };

  const handleMessage = (message: MessageEvent) => {
    try {
      const event = JSON.parse(message.data) as EventEnvelope;
      lastMessageAt.value = new Date().toISOString();
      lastError.value = "";
      onEvent(event);
    } catch (reason) {
      lastError.value =
        reason instanceof Error ? reason.message : "无法解析实时事件";
    }
  };

  const connect = () => {
    disconnect();
    if (!runId.value) return;
    stopped = false;
    const base = api.baseUrl();
    const url = `${base}/api/v1/runs/${runId.value}/stream?after_sequence=${afterSequence.value}`;
    source = new EventSource(url);
    source.onopen = () => {
      connected.value = true;
      reconnecting.value = false;
      reconnectAttempt.value = 0;
      lastError.value = "";
    };
    source.onmessage = handleMessage;
    source.onerror = () => {
      connected.value = false;
      lastError.value = navigator.onLine
        ? "实时连接中断，正在自动恢复"
        : "网络已离线，恢复后将自动补齐事件";
      source?.close();
      source = null;
      scheduleReconnect();
    };
  };

  const reconnect = () => {
    reconnectAttempt.value = 0;
    connect();
  };
  const close = () => {
    stopped = true;
    reconnecting.value = false;
    disconnect();
  };
  const online = () => reconnect();
  window.addEventListener("online", online);

  watch(runId, reconnect, { immediate: true });
  onBeforeUnmount(() => {
    close();
    window.removeEventListener("online", online);
  });
  const state = computed(() =>
    connected.value
      ? "live"
      : reconnecting.value
        ? "reconnecting"
        : "offline",
  );
  return {
    connected,
    reconnecting,
    reconnectAttempt,
    lastMessageAt,
    lastError,
    state,
    reconnect,
    close,
  };
}
