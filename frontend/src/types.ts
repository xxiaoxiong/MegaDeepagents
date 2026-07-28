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
  next_attempt_at?: string | null;
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

export interface ExecutionSummary {
  event_count: number;
  wall_time_ms: number;
  active_time_ms: number;
  parallelism: number;
  utilization: number;
  peak_concurrency: number;
  tool_call_count: number;
  retry_count: number;
  handoff_count: number;
  artifact_count: number;
  completed_tasks: number;
  total_tasks: number;
  critical_path: string[];
  critical_path_remaining: number;
}

export interface AgentExecution {
  agent_id: string;
  name: string;
  role: string;
  status: string;
  current_task_id?: string | null;
  current_task_title?: string | null;
  capabilities: string[];
  assigned_task_ids: string[];
  completed_task_ids: string[];
  artifact_ids: string[];
  event_count: number;
  tool_call_count: number;
  last_activity_at?: string | null;
  latest_summary: string;
  recent_events: EventEnvelope[];
}

export interface TaskExecution {
  task_id: string;
  title: string;
  status: string;
  claimed_by?: string | null;
  dependencies: string[];
  blocked_by: string[];
  critical: boolean;
  attempts: number;
  max_attempts: number;
  artifact_ids: string[];
  last_activity_at?: string | null;
}

export interface ExecutionAttention {
  severity: "info" | "warning" | "error";
  kind: string;
  title: string;
  detail: string;
  task_id?: string | null;
  agent_id?: string | null;
}

export interface RunExecution {
  run_id: string;
  generated_at: string;
  summary: ExecutionSummary;
  agents: AgentExecution[];
  tasks: TaskExecution[];
  attention: ExecutionAttention[];
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

export interface RunDiagnostics {
  run_id: string;
  status: string;
  health: "healthy" | "attention" | "stalled" | "failed" | "completed";
  phase: string;
  checked_at: string;
  last_activity_at?: string | null;
  silence_seconds?: number | null;
  stalled_threshold_seconds: number;
  event_count: number;
  last_sequence: number;
  latest_event?: EventEnvelope | null;
  active_assignments: Array<{
    agent_id: string;
    task_id: string;
    session_id: string;
  }>;
  task_counts: Record<string, number>;
  retryable_task_ids: string[];
  delayed_retries: Array<{
    task_id: string;
    next_attempt_at: string;
    attempt: number;
    max_attempts: number;
  }>;
  blockers: Array<{
    task_id: string;
    status: string;
    message: string;
  }>;
  recommended_action: string;
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
  diagnostics: RunDiagnostics | null;
  execution: RunExecution | null;
}

// ===== 对话式 UI：ChatMessage 模型 =====
// 把 EventEnvelope 映射成对话流中的气泡/卡片。每条消息有唯一 id（用于 Vue :key）
// 和 createdAt（排序）。discriminant 字段决定渲染组件。

export interface UserChatMessage {
  id: string;
  role: "user";
  content: string;
  createdAt: string;
}

export interface AssistantChatMessage {
  id: string;
  role: "assistant";
  content: string;
  streaming: boolean; // true 时正在累积 token，显示流式光标
  messageId?: string; // 关联 assistant_token/assistant_message 事件的 message_id
  agentId?: string | null;
  agentName?: string | null;
  createdAt: string;
}

export interface ToolCallChatMessage {
  id: string;
  type: "tool_call";
  toolCallId?: string | null;
  toolName: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  resultPreview?: string;
  durationMs?: number | null;
  agentName?: string | null;
  createdAt: string;
}

export interface ArtifactChatMessage {
  id: string;
  type: "artifact";
  artifactId: string;
  taskId?: string | null;
  producedBy?: string | null;
  createdAt: string;
}

export interface ApprovalChatMessage {
  id: string;
  type: "approval";
  kind: "permission" | "plan";
  requestId: string;
  operation?: string;
  title?: string;
  summary?: string;
  reason?: string;
  target?: string;
  status: string; // 原始状态：pending/approved/denied…
  createdAt: string;
}

export interface CollaborationChatMessage {
  id: string;
  type: "collaboration";
  fromAgent?: string | null;
  toAgent?: string | null;
  title?: string | null;
  content: string;
  taskId?: string | null;
  createdAt: string;
}

export interface StatusChatMessage {
  id: string;
  type: "status";
  text: string;
  tone: "info" | "running" | "ok" | "error" | "warn";
  createdAt: string;
}

export type ChatMessage =
  | UserChatMessage
  | AssistantChatMessage
  | ToolCallChatMessage
  | ArtifactChatMessage
  | ApprovalChatMessage
  | CollaborationChatMessage
  | StatusChatMessage;
