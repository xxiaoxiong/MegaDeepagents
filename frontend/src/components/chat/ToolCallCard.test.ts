import { createApp, h, nextTick } from "vue";
import { describe, expect, it, vi } from "vitest";
import ToolCallCard from "@/components/chat/ToolCallCard.vue";
import { api } from "@/lib/api";

describe("ToolCallCard", () => {
  it("stays a compact one-line summary until the user expands details", async () => {
    const container = document.createElement("div");
    const app = createApp({
      render: () =>
        h(ToolCallCard, {
          runId: "run_1",
          toolName: "read_file",
          args: { file_path: "README.md" },
          status: "ok",
          agentName: "Coder",
          resultPreview: "# MegaDeepagents",
          durationMs: 125,
          startedAt: "2026-07-28T08:00:00Z",
        }),
    });

    try {
      vi.spyOn(api, "workspaceFileTextContent").mockResolvedValue({
        path: "README.md",
        content: "# MegaDeepagents\ncomplete file",
        totalBytes: 31,
      });
      app.mount(container);
      const details = container.querySelector("details");
      const summary = container.querySelector("summary");
      expect(details?.open).toBe(false);
      expect(summary?.textContent).toContain("read_file");
      expect(summary?.textContent).toContain("Coder");
      expect(summary?.textContent).toContain("已完成");
      expect(summary?.textContent).toContain("125ms");
      expect(container.querySelector(".tool-details")).not.toBeNull();

      summary?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await nextTick();
      expect(details?.open).toBe(true);
      expect(container.textContent).toContain("# MegaDeepagents");
    } finally {
      app.unmount();
      vi.restoreAllMocks();
    }
  });

  it("treats timezone-less backend timestamps as UTC for live elapsed time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-28T08:00:02Z"));
    const container = document.createElement("div");
    const app = createApp({
      render: () =>
        h(ToolCallCard, {
          runId: "run_1",
          toolName: "read_file",
          args: {},
          status: "running",
          startedAt: "2026-07-28T08:00:00",
        }),
    });

    try {
      app.mount(container);
      expect(container.querySelector("summary")?.textContent).toContain("2.0s");
    } finally {
      app.unmount();
      vi.useRealTimers();
    }
  });

  it("loads the complete governed workspace file when a read tool is expanded", async () => {
    const fullContent = "line\n".repeat(2_000);
    const loader = vi.spyOn(api, "workspaceFileTextContent").mockResolvedValue({
      path: "src/large.ts",
      content: fullContent,
      totalBytes: fullContent.length,
    });
    const container = document.createElement("div");
    const app = createApp({
      render: () =>
        h(ToolCallCard, {
          runId: "run_full",
          toolName: "read_file",
          args: { file_path: "src/large.ts" },
          status: "ok",
          resultPreview: "line",
        }),
    });

    try {
      app.mount(container);
      container.querySelector("summary")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await nextTick();
      await Promise.resolve();
      await nextTick();
      expect(loader).toHaveBeenCalledWith(
        "run_full",
        "src/large.ts",
        expect.any(AbortSignal),
        expect.any(Function),
      );
      expect(container.querySelector(".tool-result-text")?.textContent).toBe(fullContent);
      expect(container.textContent).toContain("src/large.ts");
    } finally {
      app.unmount();
      vi.restoreAllMocks();
    }
  });
});
