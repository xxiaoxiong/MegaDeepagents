import { describe, expect, it } from "vitest";
import { createApp } from "vue";
import StatusBadge from "@/components/StatusBadge.vue";

describe("StatusBadge", () => {
  it("maps runtime states to a readable visual state", () => {
    const host = document.createElement("div");
    const app = createApp(StatusBadge, { status: "repair_required" });
    app.mount(host);
    expect(host.textContent).toContain("等待修复");
    expect(host.querySelector(".status-badge")?.getAttribute("data-state")).toBe(
      "warn",
    );
    app.unmount();
  });
});
