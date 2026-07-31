import { describe, expect, it } from "vitest";
import { stripThinkBlocks } from "@/lib/sanitize";

describe("stripThinkBlocks", () => {
  it("removes a complete reasoning block", () => {
    expect(
      stripThinkBlocks("before<reasoning>secret</reasoning>after"),
    ).toBe("beforeafter");
  });

  it("removes a trailing unclosed open tag", () => {
    expect(
      stripThinkBlocks("visible<reasoning>still thinking"),
    ).toBe("visible");
  });

  it("removes a stray closing tag without a matching open tag", () => {
    // Historical DB messages may carry an orphan closing tag whose opening
    // tag was lost across a token boundary or a previous message.
    expect(stripThinkBlocks("answer</reasoning>more")).toBe("answermore");
  });

  it("removes a stray opening tag alone", () => {
    expect(stripThinkBlocks("a<reasoning>b</reasoning>c")).toBe("ac");
  });

  it("preserves normal angle brackets", () => {
    expect(stripThinkBlocks("a < b and c > d")).toBe("a < b and c > d");
  });

  it("returns empty string for nullish input", () => {
    expect(stripThinkBlocks(undefined)).toBe("");
    expect(stripThinkBlocks(null)).toBe("");
    expect(stripThinkBlocks("")).toBe("");
  });

  it("handles case-insensitive tags", () => {
    expect(stripThinkBlocks("x<REASONING>z</REASONING>y")).toBe("xy");
  });
});
