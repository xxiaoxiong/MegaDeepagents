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
});
