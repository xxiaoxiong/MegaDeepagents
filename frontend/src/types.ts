export type RunMode = "auto" | "single" | "team";

export interface AgentRun {
  run_id: string;
  goal: string;
  mode: RunMode;
  resolved_mode?: string | null;
  team_template: string;
  status: string;
  workspace_root?: string;
  review_required: boolean;
  metadata: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface CreateRunInput {
  goal: string;
  mode: RunMode;
  team_template: string;
  repository_path: string | null;
  base_branch: string | null;
  review_required: boolean;
  auto_approve_low_risk: boolean;
  metadata: Record<string, unknown>;
}

export interface Task {
  task_id: string;
  run_id: string;
  title: string;
  objective: string;
  status: string;
  dependencies: string[];
  claimed_by?: string | null;
  attempts: number;
  max_attempts: number;
  last_error?: string | null;
  produced_artifact_ids: string[];
  metadata: Record<string, unknown>;
}

export interface Agent {
  agent_id: string;
  run_id: string;
  name: string;
  role: string;
  status: string;
  current_task_id?: string | null;
  capabilities: string[];
  metadata: Record<string, unknown>;
}

export interface Artifact {
  artifact_id: string;
  run_id: string;
  task_id: string;
  type: string;
  path: string;
  content_hash: string;
  size_bytes: number;
  version: number;
  produced_by: string;
  status: string;
  predecessor_id?: string | null;
  parent_artifact_id?: string | null;
  created_at?: string;
  metadata: Record<string, unknown>;
}

export interface EventEnvelope {
  event_id: string;
  run_id: string;
  agent_id?: string | null;
  task_id?: string | null;
  event_type: string;
  sequence: number;
  timestamp: string;
  trace_id?: string | null;
  payload: Record<string, unknown>;
}

export interface TaskGraph {
  root_task_id: string | null;
  version: number;
  nodes: Record<string, Record<string, unknown>>;
}

export interface PermissionRequest {
  request_id: string;
  run_id: string;
  agent_id: string;
  operation: string;
  target?: string;
  reason?: string;
  status: string;
  created_at?: string;
}

export interface PlanRequest {
  plan_id: string;
  run_id: string;
  agent_id?: string;
  title?: string;
  summary?: string;
  status: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface RunSnapshot {
  run: AgentRun | null;
  tasks: Task[];
  agents: Agent[];
  artifacts: Artifact[];
  events: EventEnvelope[];
  permissions: PermissionRequest[];
  plans: PlanRequest[];
  graph: TaskGraph | null;
  errors: Record<string, unknown>;
  git: Record<string, unknown>;
}
