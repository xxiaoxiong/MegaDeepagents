import type {
  Agent,
  AgentRun,
  Artifact,
  CreateRunInput,
  EventEnvelope,
  PermissionRequest,
  PlanRequest,
  RunDiagnostics,
  RunExecution,
  Task,
  TaskGraph,
} from "@/types";

const configuredBase = () => {
  const stored = localStorage.getItem("megadeepagents_api_base");
  return (stored ?? import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = configuredBase();
  if (
    !base &&
    import.meta.env.PROD &&
    window.location.hostname.endsWith(".vercel.app")
  ) {
    throw new ApiError(
      503,
      "前端已上线，但尚未配置持久后端。请在系统设置中填写 Runtime API 地址。",
    );
  }
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text();
    }
    const apiDetail =
      typeof detail === "object" && detail && "detail" in detail
        ? (detail as { detail: unknown }).detail
        : detail;
    const message =
      typeof apiDetail === "object" && apiDetail && "message" in apiDetail
        ? String((apiDetail as { message: unknown }).message)
        : typeof apiDetail === "string"
          ? apiDetail
          : `请求失败 (${response.status})`;
    throw new ApiError(response.status, message, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  baseUrl: configuredBase,
  listRuns: () => request<AgentRun[]>("/api/v1/runs"),
  getRun: (runId: string) => request<AgentRun>(`/api/v1/runs/${runId}`),
  createRun: (input: CreateRunInput) =>
    request<AgentRun>("/api/v1/runs", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  controlRun: (runId: string, action: "pause" | "resume" | "cancel") =>
    request<{ run_id: string; status: string }>(
      `/api/v1/runs/${runId}/${action}`,
      { method: "POST" },
    ),
  listTasks: (runId: string) =>
    request<Task[]>(`/api/v1/runs/${runId}/tasks`),
  taskGraph: (runId: string) =>
    request<TaskGraph>(`/api/v1/runs/${runId}/task-graph`),
  listAgents: (runId: string) =>
    request<Agent[]>(`/api/v1/runs/${runId}/agents`),
  execution: (runId: string) =>
    request<RunExecution>(`/api/v1/runs/${runId}/execution`),
  listArtifacts: (runId: string) =>
    request<Artifact[]>(`/api/v1/runs/${runId}/artifacts`),
  artifactContent: (runId: string, artifactId: string) =>
    request<{ content: string; truncated: boolean; path: string }>(
      `/api/v1/runs/${runId}/artifacts/${artifactId}/content`,
    ),
  artifactLineage: (runId: string, artifactId: string) =>
    request<Artifact[]>(
      `/api/v1/runs/${runId}/artifacts/${artifactId}/lineage`,
    ),
  listEvents: (runId: string, after = 0, limit = 2_000) =>
    request<EventEnvelope[]>(
      `/api/v1/runs/${runId}/events?after_sequence=${after}&limit=${limit}`,
    ),
  async listAllEvents(runId: string, cap = 20_000) {
    const events: EventEnvelope[] = [];
    let cursor = 0;
    while (events.length < cap) {
      const page = await api.listEvents(
        runId,
        cursor,
        Math.min(2_000, cap - events.length),
      );
      if (!page.length) break;
      events.push(...page);
      cursor = page.at(-1)?.sequence ?? cursor;
      if (page.length < 2_000) break;
    }
    return events;
  },
  listPermissions: (runId: string) =>
    request<PermissionRequest[]>(`/api/v1/runs/${runId}/permissions`),
  decidePermission: (
    runId: string,
    requestId: string,
    decision: "approve_once" | "approve_run" | "deny",
    reason = "",
  ) =>
    request(`/api/v1/runs/${runId}/permissions/${requestId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    }),
  listPlans: (runId: string) =>
    request<PlanRequest[]>(`/api/v1/runs/${runId}/plans`),
  decidePlan: (
    runId: string,
    planId: string,
    approved: boolean,
    feedback = "",
  ) =>
    request(`/api/v1/runs/${runId}/plans/${planId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved, feedback }),
    }),
  sendAgentMessage: (runId: string, agentId: string, content: string) =>
    request(`/api/v1/runs/${runId}/agents/${agentId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  sendRunMessage: (runId: string, content: string) =>
    request(`/api/v1/runs/${runId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  stopAgent: (runId: string, agentId: string) =>
    request(`/api/v1/runs/${runId}/agents/${agentId}/stop`, { method: "POST" }),
  errors: (runId: string) =>
    request<Record<string, unknown>>(`/api/v1/runs/${runId}/errors`),
  diagnostics: (runId: string) =>
    request<RunDiagnostics>(`/api/v1/runs/${runId}/diagnostics`),
  retryRun: (
    runId: string,
    options: { task_id?: string; reason?: string; reset_attempts?: boolean } = {},
  ) =>
    request<{
      run_id: string;
      status: string;
      retried_task_ids: string[];
      recovery_generation: number;
    }>(`/api/v1/runs/${runId}/retry`, {
      method: "POST",
      body: JSON.stringify(options),
    }),
  git: (runId: string) =>
    request<Record<string, unknown>>(`/api/v1/runs/${runId}/git`),
  settings: () => request<Record<string, unknown>>("/api/v1/settings"),
};
