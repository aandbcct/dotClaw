export type MessageRole = "user" | "assistant";

export interface SessionSummary {
  id: string;
  title: string;
  archived?: boolean;
  agent_id: string;
  model: string;
  created_at: string;
  updated_at: string;
}

export interface SessionMessage {
  id: string;
  role: MessageRole;
  content: string;
  created_at: string;
  run_ids?: string[];
}

export interface SearchHit {
  session_id: string;
  title: string;
  updated_at: string;
  archived: boolean;
  message_id: string | null;
  role: MessageRole | null;
  snippet: string;
}

export interface SearchResults {
  items: SearchHit[];
  has_more: boolean;
}

export interface SessionDetail extends SessionSummary {
  messages: SessionMessage[];
}

export interface DisplayMessage extends SessionMessage {
  reasoning?: string;
  pending?: boolean;
  execution?: MessageExecution;
  requestId?: string;
}

/** 本页消息的执行展示，不作为历史思考持久化。 */
export interface MessageExecution {
  stage: "sending" | "processing" | "thinking" | "responding" | "tool_running" | "stopping" | "completed" | "failed" | "cancelled" | "waiting_approval" | "waiting_delegation" | "recoverable" | "unknown";
  error: ErrorEnvelope["error"] | null;
  statusError: string;
  submittedText: string;
}

export interface RunStep {
  id: string;
  sequence: number;
  kind: "model" | "tool";
  status: "running" | "completed" | "failed" | "cancelled" | "approval_required" | "interrupted";
  started_at: string;
  completed_at: string;
  call_id: string | null;
}

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    retryable?: boolean;
  };
}

export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface RunAcceptedData {
  session_id: string;
  run_id: string;
}

export interface AgentOption {
  id: string;
  name: string;
  description: string;
}

export interface WorkspaceOptions {
  agents: AgentOption[];
  models: string[];
  default_agent_id: string;
  default_model: string;
}

export interface ToolExecution {
  call_id: string;
  name: string;
  status: string;
  started_at: string;
  completed_at: string;
  arguments: Record<string, unknown>;
  result: string;
  error: string;
}

export interface RunActivity {
  run_id: string;
  status: string;
  approval_id: string | null;
  child_run_id: string | null;
  tools: ToolExecution[];
  error?: ErrorEnvelope["error"] | null;
  steps?: RunStep[];
  event_sequence?: number;
}

export interface ModelSetting {
  name: string;
  provider: string;
  model_id: string;
  context_window: number;
  capabilities: string[];
  status: string;
  reasoning_mode: string;
  api_key_configured: boolean;
  preferred: boolean;
}

export interface ToolSetting {
  name: string;
  description: string;
  source: string;
  enabled: boolean;
  needs_approval: boolean;
}

export interface SkillSetting {
  name: string;
  description: string;
  emoji: string;
  enabled: boolean;
  always_load: boolean;
}

export interface McpServerSetting {
  name: string;
  transport: string;
  endpoint: string;
  state: string;
  detail: string;
  tool_count: number;
}

export interface RuntimeSettings {
  general: {
    stream: boolean;
    max_context_tokens: number;
    keep_recent_messages: number;
    exec_timeout: number;
    scheduler_enabled: boolean;
  };
  models: ModelSetting[];
  tools: ToolSetting[];
  skills: SkillSetting[];
  mcp_enabled: boolean;
  mcp_servers: McpServerSetting[];
}

export type RunPhase =
  | "idle"
  | "running"
  | "stopping"
  | "completed"
  | "failed"
  | "cancelled"
  | "suspended"
  | "recoverable";
