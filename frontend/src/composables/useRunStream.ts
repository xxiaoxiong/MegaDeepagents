import { onBeforeUnmount, ref, watch, type Ref } from "vue";
import { api } from "@/lib/api";
import type { EventEnvelope } from "@/types";

export function useRunStream(
  runId: Ref<string>,
  afterSequence: Ref<number>,
  onEvent: (event: EventEnvelope) => void,
) {
  const connected = ref(false);
  const reconnecting = ref(false);
  let source: EventSource | null = null;
  let retryTimer: number | null = null;

  const close = () => {
    source?.close();
    source = null;
    connected.value = false;
    if (retryTimer !== null) window.clearTimeout(retryTimer);
    retryTimer = null;
  };

  const connect = () => {
    close();
    if (!runId.value) return;
    const base = api.baseUrl();
    const url = `${base}/api/v1/runs/${runId.value}/stream?after_sequence=${afterSequence.value}`;
    source = new EventSource(url);
    source.onopen = () => {
      connected.value = true;
      reconnecting.value = false;
    };
    source.onmessage = (message) => {
      onEvent(JSON.parse(message.data) as EventEnvelope);
    };
    source.addEventListener("error", () => {
      connected.value = false;
      reconnecting.value = true;
      source?.close();
      retryTimer = window.setTimeout(connect, 2_000);
    });
    const knownEvents = [
      "run_started",
      "run_completed",
      "run_failed",
      "task_started",
      "task_completed",
      "task_failed",
      "artifact_created",
      "verification_completed",
    ];
    for (const eventName of knownEvents) {
      source.addEventListener(eventName, (message) => {
        onEvent(JSON.parse((message as MessageEvent).data) as EventEnvelope);
      });
    }
  };

  watch(runId, connect, { immediate: true });
  onBeforeUnmount(close);
  return { connected, reconnecting, reconnect: connect, close };
}
