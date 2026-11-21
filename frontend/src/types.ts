export type MessageRole = "user" | "assistant";

export type PersonalTaskStatus = "pending" | "in_progress" | "completed" | "cancelled";
export type PersonalTaskFilter = "active" | "all" | "completed" | "cancelled";
export type TaskDeadline = { kind: "date"; value: string } | { kind: "datetime"; value: string };
export interface ReminderChange { reminder_id: string; action: "rescheduled" | "paused" | "cancelled"; reason: string | null; next_fire_at: string | null; version: number; }
export interface ReminderSummary { active_count: number; next_fire_at: string | null; }
export interface PersonalTask { id: string; title: string; description: string; status: PersonalTaskStatus; deadline: TaskDeadline | null; created_at: string; updated_at: string; completed_at: string | null; version: number; is_overdue: boolean; reminder_changes?: ReminderChange[]; }
export interface PersonalTaskDetail extends PersonalTask { reminders: Reminder[]; }
export interface PersonalTaskListItem extends PersonalTask { reminder_summary: ReminderSummary; }
export interface PersonalTaskList { items: PersonalTaskListItem[]; timezone: string; as_of: string; total: number; offset: number; limit: number; has_more: boolean; }
export interface PersonalTaskQuery { filter: PersonalTaskFilter; keyword: string; deadline_from: string | null; deadline_until: string | null; overdue_only: boolean; offset: number; limit: number; }
export interface PersonalTaskInput { title: string; description: string; deadline: TaskDeadline | null; }
export type ReminderRule = { kind: "at"; at: string } | { kind: "before_deadline"; offset_minutes: number } | { kind: "before_date"; days_before: number; local_time: string } | { kind: "daily"; local_time: string } | { kind: "weekly"; weekdays: number[]; local_time: string } | { kind: "monthly"; day: number; local_time: string } | { kind: "yearly"; month: number; day: number; local_time: string };
export interface Reminder { id: string; task_id: string; rule: ReminderRule; timezone: string; status: string; reason: string | null; next_fire_at: string | null; version: number; created_at: string; updated_at: string; }
export interface ReminderList { items: Reminder[]; timezone: string; scheduler_enabled: boolean; last_scan_at: string | null; scan_lag_seconds: number | null; }
export interface NotificationMember { task_id: string; title_snapshot: string; missed: boolean; overdue: boolean; current_status: string | null; current_is_overdue: boolean | null; }
export interface NotificationDelivery { notification_id: number; channel: string; claim_id: string; status: string; attempted_at: string; reported_at: string | null; error: string | null; }
export interface NotificationItem { id: number; kind: "reminder" | "recovery_summary"; occurrence_id: string | null; created_at: string; read_at: string | null; title: string; body: string; members: NotificationMember[]; deliveries: NotificationDelivery[]; }
export interface NotificationList { items: NotificationItem[]; next_before_id: number | null; unread_count: number; watermark: number; }
export interface NotificationChanges { items: NotificationItem[]; next_after_id: number; has_more: boolean; unread_count: number; watermark: number; }

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
  stage: "sending" | "processing" | "thinking" | "responding" | "tool_running" | "stopping" | "completed" | "failed" | "cancelled" | "abandoned" | "waiting_approval" | "waiting_delegation" | "recoverable" | "unknown";
  error: ErrorEnvelope["error"] | null;
  statusError: string;
  submittedText: string;
}

export interface RunStep {
  id: string;
  sequence: number;
  kind: "model" | "tool";
  status: "running" | "completed" | "failed" | "cancelled" | "approval_required" | "interrupted" | "uncertain";
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
    id?: string;
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
  approval_call_id?: string | null;
  child_run_id: string | null;
  tools: ToolExecution[];
  error?: ErrorEnvelope["error"] | null;
  steps?: RunStep[];
  event_sequence?: number;
    control_revision?: number;
    generation?: string;
    committing?: boolean;
    cancellation?: {
      request_id: string; requested_at: string; phase: "requested" | "draining" | "blocked" | "settled";
      pending_operation_count: number; pending_child_count: number; effect: "local_only" | "remote_unknown";
      diagnostic: string | null;
    } | null;
}

export interface CancelReceipt {
  run_id: string; disposition: "accepted" | "already_requested" | "already_ended" | "committing_success" | "not_found";
  request_id: string | null; control_revision: number; outcome: string | null; requested_at: string | null;
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
