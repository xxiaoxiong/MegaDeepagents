import { expect, test, type Page, type Route } from "@playwright/test";

const run = {
  run_id: "run_e2e",
  goal: "Refactor the runtime and verify the complete artifact experience",
  mode: "team",
  resolved_mode: "team",
  team_template: "software_dev_team",
  status: "running",
  review_required: true,
  metadata: {},
  created_at: "2026-07-31T08:00:00Z",
  updated_at: "2026-07-31T08:05:00Z",
};

const artifact = {
  artifact_id: "artifact_opaque_42",
  run_id: run.run_id,
  task_id: "task_review",
  type: "document",
  path: "reports/runtime-audit.md",
  content_hash: "1234567890abcdef",
  size_bytes: 700_000,
  version: 2,
  produced_by: "Reviewer",
  status: "published",
  metadata: {},
};

const events = [
  {
    event_id: "e1",
    run_id: run.run_id,
    event_type: "user_message",
    sequence: 1,
    timestamp: "2026-07-31T08:00:00Z",
    payload: { content: "Audit and improve the complete runtime." },
  },
  {
    event_id: "e2",
    run_id: run.run_id,
    agent_id: "coder_1",
    task_id: "task_frontend",
    event_type: "tool_call_started",
    sequence: 2,
    timestamp: "2026-07-31T08:01:00Z",
    payload: {
      tool_call_id: "tc_read",
      tool_name: "read_file",
      arguments: { file_path: "src/large-runtime.ts" },
      agent_name: "Coder-1",
    },
  },
  {
    event_id: "e3",
    run_id: run.run_id,
    agent_id: "coder_1",
    task_id: "task_frontend",
    event_type: "tool_call_result",
    sequence: 3,
    timestamp: "2026-07-31T08:01:01Z",
    payload: {
      tool_call_id: "tc_read",
      tool_name: "read_file",
      result_preview: "export const preview = true;",
      status: "ok",
      duration_ms: 84,
      agent_name: "Coder-1",
    },
  },
  {
    event_id: "e4",
    run_id: run.run_id,
    agent_id: "reviewer_1",
    task_id: "task_review",
    event_type: "TaskProduced",
    sequence: 4,
    timestamp: "2026-07-31T08:02:00Z",
    payload: { artifact_ids: [artifact.artifact_id], agent_name: "Reviewer" },
  },
];

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockRuntime(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/runs") return json(route, [run]);
    if (path === `/api/v1/runs/${run.run_id}/events`) return json(route, events);
    if (path === `/api/v1/runs/${run.run_id}/stream`) {
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": ready\n\n" });
    }
    if (path === `/api/v1/runs/${run.run_id}/execution`) {
      return json(route, {
        run_id: run.run_id,
        generated_at: "2026-07-31T08:05:00Z",
        summary: {
          event_count: events.length,
          wall_time_ms: 300_000,
          active_time_ms: 480_000,
          parallelism: 1.6,
          utilization: 0.8,
          peak_concurrency: 2,
          tool_call_count: 7,
          retry_count: 1,
          handoff_count: 1,
          artifact_count: 1,
          completed_tasks: 1,
          total_tasks: 4,
          critical_path: ["task_plan", "task_frontend", "task_review"],
          critical_path_remaining: 1,
        },
        agents: [
          {
            agent_id: "coder_1",
            name: "Coder-1",
            role: "Coder",
            status: "running",
            current_task_id: "task_frontend",
            current_task_title: "Refactor artifact UI",
            capabilities: ["coding"],
            assigned_task_ids: ["task_frontend"],
            completed_task_ids: [],
            artifact_ids: [],
            event_count: 3,
            tool_call_count: 4,
            latest_summary: "Editing the artifact drawer",
            recent_events: [],
          },
          {
            agent_id: "coder_2",
            name: "Coder-2",
            role: "Coder",
            status: "running",
            current_task_id: "task_backend",
            current_task_title: "Harden content API",
            capabilities: ["coding"],
            assigned_task_ids: ["task_backend"],
            completed_task_ids: [],
            artifact_ids: [],
            event_count: 3,
            tool_call_count: 3,
            latest_summary: "Adding chunked reads",
            recent_events: [],
          },
        ],
        tasks: [],
        attention: [],
      });
    }
    if (path === `/api/v1/runs/${run.run_id}/artifacts`) return json(route, [artifact]);
    if (path.endsWith(`/${artifact.artifact_id}/lineage`)) return json(route, [artifact]);
    if (path.endsWith(`/${artifact.artifact_id}/content`)) {
      const offset = Number(url.searchParams.get("offset") || 0);
      return json(
        route,
        offset === 0
          ? {
              artifact_id: artifact.artifact_id,
              path: artifact.path,
              content: "# Runtime audit\n\nFirst chunk.\n",
              encoding: "utf-8",
              truncated: true,
              offset: 0,
              next_offset: 32,
              total_bytes: 64,
              complete: false,
            }
          : {
              artifact_id: artifact.artifact_id,
              path: artifact.path,
              content: "Second chunk.\nFULL_ARTIFACT_TAIL",
              encoding: "utf-8",
              truncated: false,
              offset: 32,
              next_offset: null,
              total_bytes: 64,
              complete: true,
            },
      );
    }
    if (path === `/api/v1/runs/${run.run_id}/files/content`) {
      const offset = Number(url.searchParams.get("offset") || 0);
      return json(
        route,
        offset === 0
          ? {
              path: "src/large-runtime.ts",
              content: "export const start = true;\n",
              encoding: "utf-8",
              truncated: true,
              offset: 0,
              next_offset: 28,
              total_bytes: 60,
              complete: false,
            }
          : {
              path: "src/large-runtime.ts",
              content: "export const FULL_FILE_TAIL = true;",
              encoding: "utf-8",
              truncated: false,
              offset: 28,
              next_offset: null,
              total_bytes: 60,
              complete: true,
            },
      );
    }
    return json(route, {});
  });
}

test.beforeEach(async ({ page }) => {
  await mockRuntime(page);
  await page.goto(`/chat/${run.run_id}`);
  await expect(page.getByText("runtime-audit.md", { exact: true })).toBeVisible();
});

test("desktop keeps activity compact and opens the complete artifact", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium", "desktop layout assertion");
  await expect(page.getByText("2 个 Agent 并行中")).toBeVisible();

  const artifactCard = page.locator(".artifact-card");
  const toolCard = page.locator(".tool-call-card");
  expect((await artifactCard.boundingBox())?.width).toBeLessThan(480);
  expect((await toolCard.boundingBox())?.width).toBeLessThan(520);
  await expect(artifactCard).not.toContainText(artifact.artifact_id);

  await artifactCard.click();
  await expect(page.locator(".chat-drawer")).toBeVisible();
  await expect(page.locator(".artifact-preview-content")).toContainText("FULL_ARTIFACT_TAIL");
  expect((await page.locator(".chat-drawer").boundingBox())?.width).toBeGreaterThan(470);
  await page.screenshot({ path: "test-results/visual/chat-artifact-open.png", fullPage: true });

  await page.locator(".chat-drawer-head button").click();
  await toolCard.locator(".tool-head").click();
  await expect(toolCard.locator(".tool-result-text")).toContainText("FULL_FILE_TAIL");
  await expect(toolCard.locator(".tool-result > header strong")).toHaveText("src/large-runtime.ts");
  await page.screenshot({ path: "test-results/visual/chat-tool-expanded.png", fullPage: true });
});

test("mobile artifact drawer owns the viewport without horizontal clipping", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium", "mobile layout assertion");
  await page.locator(".artifact-card").click();
  await expect(page.locator(".chat-drawer")).toBeVisible();
  await expect(page.locator(".artifact-preview-content")).toContainText("FULL_ARTIFACT_TAIL");
  const viewportWidth = page.viewportSize()?.width ?? 0;
  const drawer = await page.locator(".chat-drawer").boundingBox();
  expect(drawer?.x ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(1);
  expect(Math.abs((drawer?.width ?? 0) - viewportWidth)).toBeLessThanOrEqual(1);
  const viewport = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    visualWidth: window.visualViewport?.width ?? window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(Math.abs(viewport.innerWidth - viewportWidth)).toBeLessThanOrEqual(1);
  expect(Math.abs(viewport.visualWidth - viewportWidth)).toBeLessThanOrEqual(1);
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewportWidth + 1);
  await page.screenshot({ path: "test-results/visual/chat-artifact-mobile.png" });
});
