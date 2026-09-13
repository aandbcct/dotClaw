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
}

export interface SessionDetail extends SessionSummary {
  messages: SessionMessage[];
}

export interface DisplayMessage extends SessionMessage {
  reasoning?: string;
  pending?: boolean;
}

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
  };
}

export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
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
