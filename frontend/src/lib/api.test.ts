import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "@/lib/api";

describe("v1 API client", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("uses the configured backend and serializes a real create request", async () => {
    localStorage.setItem("megadeepagents_api_base", "https://runtime.example/");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ run_id: "run_1", status: "running", goal: "Ship it" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );

    await api.createRun({
      goal: "Ship it",
      mode: "auto",
      team_template: "software_dev_team",
      repository_path: null,
      base_branch: null,
      review_required: true,
      auto_approve_low_risk: false,
      metadata: {},
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://runtime.example/api/v1/runs");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toMatchObject({ mode: "auto" });
  });

  it("surfaces the API error detail", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Run not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const error = await api.getRun("missing").catch((reason) => reason as ApiError);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      message: "Run not found",
    });
  });

  it("follows artifact cursors until the complete text is available", async () => {
    const progress = vi.fn();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const second = url.includes("offset=4");
      return new Response(
        JSON.stringify(
          second
            ? {
                artifact_id: "art_1",
                path: "report.md",
                content: "完整",
                encoding: "utf-8",
                truncated: false,
                offset: 4,
                next_offset: null,
                total_bytes: 10,
                complete: true,
              }
            : {
                artifact_id: "art_1",
                path: "report.md",
                content: "head",
                encoding: "utf-8",
                truncated: true,
                offset: 0,
                next_offset: 4,
                total_bytes: 10,
                complete: false,
              },
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    const content = await api.artifactTextContent("run_1", "art_1", undefined, progress);

    expect(content).toEqual({ path: "report.md", content: "head完整", totalBytes: 10 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toContain("offset=4");
    expect(progress).toHaveBeenLastCalledWith(10, 10);
  });
});
