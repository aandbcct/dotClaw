export type MessageRole = "user" | "assistant";

export interface SessionSummary {
  id: string;
  title: string;
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

export type RunPhase =
  | "idle"
  | "running"
  | "stopping"
  | "completed"
  | "failed"
  | "cancelled"
  | "suspended";
