import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import { useRunsStore } from "@/stores/runs";
import type { EventEnvelope } from "@/types";

const event = (sequence: number): EventEnvelope => ({
  event_id: `evt_${sequence}`,
  run_id: "run_1",
  event_type: "task_started",
  sequence,
  timestamp: "2026-07-24T10:00:00Z",
  payload: {},
});

describe("run event reducer", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("deduplicates replayed SSE envelopes and preserves sequence order", () => {
    const store = useRunsStore();
    store.applyEvent(event(2));
    store.applyEvent(event(1));
    store.applyEvent(event(2));

    expect(store.current.events.map((item) => item.sequence)).toEqual([1, 2]);
    expect(store.lastSequence).toBe(2);
  });
});
