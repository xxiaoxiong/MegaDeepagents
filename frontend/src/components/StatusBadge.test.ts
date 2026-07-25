import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import StatusBadge from "@/components/StatusBadge.vue";

describe("StatusBadge", () => {
  it("maps runtime states to a readable visual state", () => {
    const wrapper = mount(StatusBadge, { props: { status: "repair_required" } });
    expect(wrapper.text()).toContain("等待修复");
    expect(wrapper.attributes("data-state")).toBe("warn");
  });
});
