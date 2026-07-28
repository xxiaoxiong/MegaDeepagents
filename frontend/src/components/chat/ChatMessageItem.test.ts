import { createApp, h } from "vue";
import { describe, expect, it } from "vitest";
import ChatMessageItem from "@/components/chat/ChatMessageItem.vue";
import type { ChatMessage } from "@/types";

function render(message: ChatMessage) {
  const container = document.createElement("div");
  const app = createApp({
    render: () =>
      h(ChatMessageItem, {
        message,
        runId: "run_v3",
      }),
  });
  app.mount(container);
  return { app, container };
}

describe("ChatMessageItem multi-agent identity", () => {
  it("renders the assistant agent name as visible text", () => {
    const { app, container } = render({
      id: "message_1",
      role: "assistant",
      content: "我正在实现任务。",
      streaming: false,
      agentId: "agent_coder",
      agentName: "Coder",
      createdAt: "2026-07-24T10:00:00Z",
    });
    try {
      expect(container.querySelector(".assistant-agent-name")?.textContent).toContain(
        "Coder",
      );
      expect(container.textContent).toContain("我正在实现任务。");
    } finally {
      app.unmount();
    }
  });

  it("renders an explicit from-to collaboration route", () => {
    const { app, container } = render({
      id: "message_2",
      type: "collaboration",
      fromAgent: "Coder",
      toAgent: "Reviewer",
      title: "请求复核",
      content: "补丁已提交。",
      taskId: "task_code",
      createdAt: "2026-07-24T10:00:01Z",
    });
    try {
      expect(container.textContent).toContain("Coder");
      expect(container.textContent).toContain("Reviewer");
      expect(container.textContent).toContain("补丁已提交。");
      expect(container.textContent).toContain("task_code");
    } finally {
      app.unmount();
    }
  });
});
