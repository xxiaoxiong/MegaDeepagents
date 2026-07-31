import { createApp, h } from "vue";
import { describe, expect, it, vi } from "vitest";
import ArtifactCard from "@/components/chat/ArtifactCard.vue";

describe("ArtifactCard", () => {
  it("uses the filename as the compact primary action instead of an opaque id", () => {
    const container = document.createElement("div");
    const onOpen = vi.fn();
    const app = createApp({
      render: () =>
        h(ArtifactCard, {
          runId: "run_1",
          artifactId: "artifact_4d5f6a",
          path: "reports/runtime-audit.md",
          artifactType: "document",
          sizeBytes: 4_096,
          producedBy: "Reviewer",
          onOpen,
        }),
    });

    try {
      app.mount(container);
      const button = container.querySelector("button") as HTMLButtonElement;
      expect(button.textContent).toContain("runtime-audit.md");
      expect(button.textContent).not.toContain("artifact_4d5f6a");
      expect(button.textContent).toContain("4.0 KiB");
      button.click();
      expect(onOpen).toHaveBeenCalledWith("artifact_4d5f6a");
    } finally {
      app.unmount();
    }
  });
});
