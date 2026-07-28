import { createApp, h, nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  mapEventsToMessages,
  useChatThread,
} from "@/composables/useChatThread";
import { api } from "@/lib/api";
import type { EventEnvelope } from "@/types";

let seq = 0;
function env(
  event_type: string,
  payload: Record<string, unknown>,
  overrides: Partial<EventEnvelope> = {},
): EventEnvelope {
  seq += 1;
  return {
    event_id: `evt_${seq}`,
    run_id: "run_test",
    event_type,
    sequence: seq,
    timestamp: new Date(2026, 0, 1, 0, 0, seq).toISOString(),
    payload,
    ...overrides,
  };
}

function resetSeq() {
  seq = 0;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("mapEventsToMessages", () => {
  it("maps user_message to a user bubble", () => {
    resetSeq();
    const events = [env("user_message", { content: "Hello Agent" })];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: "user",
      content: "Hello Agent",
    });
  });

  it("accumulates assistant_token deltas into one streaming bubble by message_id", () => {
    resetSeq();
    const events = [
      env("assistant_token", { message_id: "m1", delta: "Hello", agent_name: "Coder" }),
      env("assistant_token", { message_id: "m1", delta: ", " }),
      env("assistant_token", { message_id: "m1", delta: "world!" }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    const m = messages[0];
    expect(m).toMatchObject({
      role: "assistant",
      content: "Hello, world!",
      streaming: true,
      agentName: "Coder",
    });
  });

  it("finalizes the assistant bubble on assistant_message (streaming=false, content overridden)", () => {
    resetSeq();
    const events = [
      env("assistant_token", { message_id: "m1", delta: "draft" }),
      env("assistant_message", { message_id: "m1", content: "final answer" }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: "assistant",
      content: "final answer",
      streaming: false,
    });
  });

  it("emits a standalone assistant_message even without preceding tokens", () => {
    resetSeq();
    const events = [
      env("assistant_message", { message_id: "m9", content: "hi" }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: "assistant",
      content: "hi",
      streaming: false,
    });
  });

  it("merges tool_call_started and tool_call_result by tool_call_id", () => {
    resetSeq();
    const events = [
      env("tool_call_started", {
        tool_call_id: "tc1",
        tool_name: "read_file",
        arguments: { path: "/a.py" },
        agent_name: "Coder",
      }),
      env("tool_call_result", {
        tool_call_id: "tc1",
        tool_name: "read_file",
        result_preview: "print('hi')",
        status: "ok",
        duration_ms: 120,
      }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      type: "tool_call",
      toolName: "read_file",
      status: "ok",
      resultPreview: "print('hi')",
      durationMs: 120,
    });
  });

  it("keeps a tool_call as running when only tool_call_started arrived", () => {
    resetSeq();
    const events = [
      env("tool_call_started", {
        tool_call_id: "tc2",
        tool_name: "shell",
        arguments: {},
      }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      type: "tool_call",
      status: "running",
    });
  });

  it("marks a tool_call as error when status is error", () => {
    resetSeq();
    const events = [
      env("tool_call_started", { tool_call_id: "tc3", tool_name: "shell", arguments: {} }),
      env("tool_call_result", {
        tool_call_id: "tc3",
        tool_name: "shell",
        status: "error",
        result_preview: "boom",
        duration_ms: 5,
      }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages[0]).toMatchObject({ type: "tool_call", status: "error" });
  });

  it("maps artifact_created to an artifact card", () => {
    resetSeq();
    const events = [
      env("artifact_created", {
        artifact_id: "art_1",
        produced_by: "Coder",
      }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      type: "artifact",
      artifactId: "art_1",
      producedBy: "Coder",
    });
  });

  it("maps task_started / task_terminated to status pills with proper tones", () => {
    resetSeq();
    const events = [
      env("task_started", { goal: "do something hard here please" }),
      env("task_terminated", { status: "completed", total_rounds: 3, elapsed: 42 }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(2);
    expect(messages[0]).toMatchObject({
      type: "status",
      tone: "running",
    });
    expect((messages[0] as { text: string }).text).toContain("do something");
    expect(messages[1]).toMatchObject({
      type: "status",
      tone: "ok",
    });
  });

  it("marks cancelled tasks as warn and failed tasks as error", () => {
    resetSeq();
    const cancelled = mapEventsToMessages([
      env("task_terminated", { status: "cancelled", total_rounds: 1, elapsed: 2 }),
    ]);
    expect(cancelled[0]).toMatchObject({ type: "status", tone: "warn" });

    resetSeq();
    const failed = mapEventsToMessages([
      env("task_terminated", { status: "failed", total_rounds: 4, elapsed: 99 }),
    ]);
    expect(failed[0]).toMatchObject({ type: "status", tone: "error" });
  });

  it("surfaces budget, replan, and stale-result control events", () => {
    resetSeq();
    const messages = mapEventsToMessages([
      env(
        "TaskBudgetExceeded",
        { used: 8, limit: 8 },
        { agent_id: "Coder", task_id: "task_code" },
      ),
      env(
        "ReplanRequested",
        { reason: "dependency changed" },
        { agent_id: "Reviewer", task_id: "task_review" },
      ),
      env(
        "TaskStateConflict",
        { transition: "mark_produced", actual_status: "cancelled" },
        { task_id: "task_late" },
      ),
    ]);

    expect(messages).toHaveLength(3);
    expect(messages[0]).toMatchObject({ type: "status", tone: "error" });
    expect((messages[0] as { text: string }).text).toContain("8/8");
    expect(messages[1]).toMatchObject({ type: "status", tone: "warn" });
    expect((messages[1] as { text: string }).text).toContain("dependency changed");
    expect(messages[2]).toMatchObject({ type: "status", tone: "error" });
    expect((messages[2] as { text: string }).text).toContain("cancelled");
  });

  it("surfaces repository integration verification progress and recovery", () => {
    resetSeq();
    const messages = mapEventsToMessages([
      env("IntegrationVerificationStarted", {
        label: "frontend npm test",
      }),
      env("IntegrationVerificationCompleted", {
        label: "frontend npm test",
        returncode: 0,
        duration_seconds: 2.4,
      }),
      env("IntegrationVerificationUnavailable", {
        missing_requirements: ["frontend:npm_runtime_unavailable"],
      }),
    ]);

    expect(messages).toHaveLength(3);
    expect(messages[0]).toMatchObject({ type: "status", tone: "running" });
    expect((messages[0] as { text: string }).text).toContain(
      "frontend npm test",
    );
    expect(messages[1]).toMatchObject({ type: "status", tone: "ok" });
    expect((messages[1] as { text: string }).text).toContain("2.4");
    expect(messages[2]).toMatchObject({ type: "status", tone: "warn" });
    expect((messages[2] as { text: string }).text).toContain(
      "npm_runtime_unavailable",
    );
  });

  it("sorts out-of-order events by sequence", () => {
    resetSeq();
    const e1 = env("user_message", { content: "first" });
    const e2 = env("assistant_message", { message_id: "m", content: "second" });
    // 故意打乱顺序传入
    const messages = mapEventsToMessages([e2, e1]);
    expect(messages.map((m) => ("role" in m ? m.role : m.type))).toEqual([
      "user",
      "assistant",
    ]);
  });

  it("deduplicates events by event_id", () => {
    resetSeq();
    const e = env("user_message", { content: "dup" });
    const messages = mapEventsToMessages([e, { ...e }]);
    expect(messages).toHaveLength(1);
  });

  it("ignores noise events (actions_emitted, round_started, etc.)", () => {
    resetSeq();
    const events = [
      env("actions_emitted", { foo: "bar" }),
      env("round_started", { round: 1 }),
      env("state_updated", { x: 1 }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages).toHaveLength(0);
  });

  it("renders a full conversation: user → assistant tokens → tool call → assistant final", () => {
    resetSeq();
    // 真实流程：第一轮 LLM 输出 token（m1）+ 工具调用；工具返回后第二轮 LLM 输出（m2）
    const events = [
      env("user_message", { content: "请读取 README" }),
      env("assistant_token", { message_id: "m1", delta: "好的，我来", agent_name: "Coder" }),
      env("assistant_token", { message_id: "m1", delta: "读取 README。" }),
      env("assistant_message", { message_id: "m1", content: "好的，我来读取 README。" }),
      env("tool_call_started", {
        tool_call_id: "tc1",
        tool_name: "read_file",
        arguments: { path: "README.md" },
        agent_name: "Coder",
      }),
      env("tool_call_result", {
        tool_call_id: "tc1",
        tool_name: "read_file",
        result_preview: "# MegaDeepagents",
        status: "ok",
        duration_ms: 88,
      }),
      env("assistant_token", { message_id: "m2", delta: "README 已读取", agent_name: "Coder" }),
      env("assistant_message", { message_id: "m2", content: "README 已读取，这是 MegaDeepagents 项目。" }),
    ];
    const messages = mapEventsToMessages(events);
    expect(messages.map((m) => ("role" in m ? m.role : m.type))).toEqual([
      "user",
      "assistant",
      "tool_call",
      "assistant",
    ]);
    // 第一条 assistant 已定稿（streaming=false）
    expect(messages[1]).toMatchObject({
      role: "assistant",
      content: "好的，我来读取 README。",
      streaming: false,
    });
    // 第二条 assistant 是工具返回后的最终答复
    const finalAssistant = messages[3];
    expect(finalAssistant).toMatchObject({
      role: "assistant",
      content: "README 已读取，这是 MegaDeepagents 项目。",
      streaming: false,
    });
  });

  it("projects a canonical V3 team trace into tasks, artifacts, approvals, collaboration and terminal states", () => {
    resetSeq();
    const events = [
      env(
        "TaskStarted",
        {},
        { agent_id: "agent_coder", task_id: "task_code" },
      ),
      env(
        "TaskProduced",
        { artifact_ids: ["artifact_patch", "artifact_tests"] },
        { agent_id: "agent_coder", task_id: "task_code" },
      ),
      // A mixed-version deployment may also replay the legacy artifact event;
      // the projection must not show the same deliverable twice.
      env("artifact_created", {
        artifact_id: "artifact_patch",
        produced_by: "Coder",
      }),
      env(
        "TaskCompleted",
        {},
        { agent_id: "agent_coder", task_id: "task_code" },
      ),
      env(
        "PermissionRequested",
        {
          request_id: "permission_1",
          operation: "shell",
          status: "pending",
          reason: "需要运行测试",
          parameters: { command: "npm test" },
        },
        { agent_id: "agent_coder", task_id: "task_code" },
      ),
      env(
        "AgentMessage",
        {
          from_agent_name: "Coder",
          to_agent_name: "Reviewer",
          title: "请求复核",
          content: "补丁和测试已经就绪。",
        },
        { agent_id: "agent_coder", task_id: "task_code" },
      ),
      env("root_graph:RunCompleted", { summary: "全部任务验证通过" }),
      env(
        "TaskFailed",
        { error: "lint failed" },
        { agent_id: "agent_reviewer", task_id: "task_review" },
      ),
      env("RunFailed", { error: "verification budget exhausted" }),
    ];

    const messages = mapEventsToMessages(events);
    expect(messages.map((message) => (
      "role" in message ? message.role : message.type
    ))).toEqual([
      "status",
      "artifact",
      "artifact",
      "status",
      "approval",
      "collaboration",
      "status",
      "status",
      "status",
    ]);
    expect(messages[0]).toMatchObject({
      type: "status",
      tone: "running",
      text: "agent_coder 开始执行 · task_code",
    });
    expect(messages[1]).toMatchObject({
      type: "artifact",
      artifactId: "artifact_patch",
      producedBy: "agent_coder",
    });
    expect(messages[4]).toMatchObject({
      type: "approval",
      requestId: "permission_1",
      operation: "shell",
      status: "pending",
    });
    expect(messages[5]).toMatchObject({
      type: "collaboration",
      fromAgent: "Coder",
      toAgent: "Reviewer",
      content: "补丁和测试已经就绪。",
    });
    expect(messages[6]).toMatchObject({
      type: "status",
      tone: "ok",
      text: "全部任务验证通过",
    });
    expect(messages[7]).toMatchObject({
      type: "status",
      tone: "error",
    });
    expect(messages[8]).toMatchObject({
      type: "status",
      tone: "error",
      text: "运行失败：verification budget exhausted",
    });
  });

  it("renders every live token through Vue and starts SSE after history replay", async () => {
    resetSeq();
    const historyEvent = env("user_message", { content: "stream please" });
    vi.spyOn(api, "listAllEvents").mockResolvedValue([historyEvent]);

    class FakeEventSource {
      static instances: FakeEventSource[] = [];
      onopen: (() => void) | null = null;
      onmessage: ((message: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      closed = false;

      constructor(public readonly url: string) {
        FakeEventSource.instances.push(this);
      }

      close() {
        this.closed = true;
      }

      emit(event: EventEnvelope) {
        this.onmessage?.({
          data: JSON.stringify(event),
        } as MessageEvent);
      }
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    const runId = ref("run_test");
    let thread!: ReturnType<typeof useChatThread>;
    const container = document.createElement("div");
    const app = createApp({
      setup() {
        thread = useChatThread(runId);
        return () =>
          h(
            "div",
            thread.messages.value.map((message) =>
              "role" in message && message.role === "assistant"
                ? h(
                    "p",
                    { "data-streaming": String(message.streaming) },
                    message.content,
                  )
                : h("p", "role" in message ? message.content : message.type),
            ),
          );
      },
    });

    try {
      app.mount(container);
      await vi.waitFor(() => {
        expect(FakeEventSource.instances).toHaveLength(1);
      });
      const source = FakeEventSource.instances[0];
      expect(source.url).toContain("after_sequence=1");
      source.onopen?.();

      source.emit(
        env("assistant_token", { message_id: "live", delta: "实时" }),
      );
      await nextTick();
      expect(container.textContent).toContain("实时");
      expect(container.querySelector("p[data-streaming='true']")).not.toBeNull();

      // A dropped transport closes the cursor, but the first token delivered
      // after recovery must resume the same bubble instead of leaving it in a
      // visually finalized state.
      source.onerror?.();
      await nextTick();
      expect(container.querySelector("p[data-streaming='false']")).not.toBeNull();
      source.emit(
        env("assistant_token", { message_id: "live", delta: "内容持续增长" }),
      );
      await nextTick();
      expect(container.textContent).toContain("实时内容持续增长");
      expect(container.querySelector("p[data-streaming='true']")).not.toBeNull();

      source.emit(
        env("assistant_message", {
          message_id: "live",
          content: "实时内容持续增长并完整结束。",
        }),
      );
      await nextTick();
      expect(container.textContent).toContain("实时内容持续增长并完整结束。");
      expect(container.querySelector("p[data-streaming='false']")).not.toBeNull();
      expect(thread.afterSequence.value).toBe(4);
    } finally {
      app.unmount();
    }
  });
});
