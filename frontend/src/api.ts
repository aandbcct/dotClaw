import type {
  ErrorEnvelope,
  SessionDetail,
  SessionSummary,
  StreamEvent,
  RunActivity,
  RuntimeSettings,
  SearchResults,
  WorkspaceOptions,
  CancelReceipt,
} from "./types";

const API_ROOT = "/api/v1";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;

  constructor(message: string, code: string, status: number, retryable = false) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

async function requestJson<T>(
  input: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as T;
}

async function responseError(response: Response): Promise<ApiError> {
  try {
    const payload = (await response.json()) as ErrorEnvelope;
    return new ApiError(
      payload.error.message,
      payload.error.code,
      response.status,
      payload.error.retryable === true,
    );
  } catch {
    return new ApiError(`服务器返回 HTTP ${response.status}，请查看运行状态`, "http_error", response.status);
  }
}

export function listSessions(): Promise<SessionSummary[]> {
  return requestJson<SessionSummary[]>(`${API_ROOT}/sessions`);
}

export function getSession(sessionId: string): Promise<SessionDetail> {
  return requestJson<SessionDetail>(`${API_ROOT}/sessions/${sessionId}`);
}

export function switchSessionModel(sessionId: string, model: string): Promise<SessionSummary> {
  return requestJson<SessionSummary>(`${API_ROOT}/sessions/${sessionId}/model`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }),
  });
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_ROOT}/sessions/${sessionId}`, { method: "DELETE" });
  if (!response.ok) throw await responseError(response);
}

export function searchSessions(query: string, signal?: AbortSignal): Promise<SearchResults> {
  return requestJson<SearchResults>(`${API_ROOT}/sessions/search?${new URLSearchParams({ q: query, archived: "all", limit: "30" })}`, { signal });
}

export function updateSession(
  sessionId: string,
  changes: { title?: string; archived?: boolean },
): Promise<SessionSummary> {
  return requestJson<SessionSummary>(`${API_ROOT}/sessions/${sessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

export function getWorkspaceOptions(): Promise<WorkspaceOptions> {
  return requestJson<WorkspaceOptions>(`${API_ROOT}/options`);
}

export function getRuntimeSettings(): Promise<RuntimeSettings> {
  return requestJson<RuntimeSettings>(`${API_ROOT}/settings`);
}

export function toggleTool(name: string, enabled: boolean): Promise<RuntimeSettings> {
  return requestJson<RuntimeSettings>(`${API_ROOT}/settings/tools/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function toggleSkill(name: string, enabled: boolean): Promise<RuntimeSettings> {
  return requestJson<RuntimeSettings>(`${API_ROOT}/settings/skills/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function reconnectMcp(): Promise<RuntimeSettings> {
  return requestJson<RuntimeSettings>(`${API_ROOT}/settings/mcp/reconnect`, { method: "POST" });
}

export function createSession(options?: {
  agent_id?: string;
  model?: string;
}): Promise<SessionSummary> {
  return requestJson<SessionSummary>(`${API_ROOT}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "新对话", ...options }),
  });
}

export async function getRunActivity(
  sessionId: string,
  runId?: string,
): Promise<RunActivity | null> {
  const query = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  const activity = await requestJson<RunActivity | null>(
    `${API_ROOT}/sessions/${sessionId}/activity${query}`,
  );
  if (activity === null) return null;
  return validateRunActivity(activity);
}

function validateRunActivity(activity: RunActivity): RunActivity {
  if (!isRecord(activity) || !Array.isArray(activity.tools) || typeof activity.run_id !== "string" || !activity.run_id
    || !["running", "completed", "failed", "cancelled", "abandoned", "waiting_approval", "waiting_delegation", "recoverable", "cancelling"].includes(activity.status)) {
    throw new ApiError("运行状态数据格式无效", "invalid_activity", 200);
  }
  if (activity.approval_call_id !== undefined && activity.approval_call_id !== null && typeof activity.approval_call_id !== "string") throw new ApiError("审批关联数据格式无效", "invalid_activity", 200);
  if ((activity.control_revision !== undefined && (!Number.isSafeInteger(activity.control_revision) || activity.control_revision < 0))
    || (activity.generation !== undefined && typeof activity.generation !== "string")
    || (activity.committing !== undefined && typeof activity.committing !== "boolean")) throw new ApiError("运行控制游标格式无效", "invalid_activity", 200);
  const cancellation = activity.cancellation;
  if (cancellation !== undefined && cancellation !== null && (!isRecord(cancellation) || !["requested", "draining", "blocked", "settled"].includes(cancellation.phase)
    || !["local_only", "remote_unknown"].includes(cancellation.effect) || typeof cancellation.request_id !== "string"
    || typeof cancellation.requested_at !== "string" || !Number.isSafeInteger(cancellation.pending_operation_count) || cancellation.pending_operation_count < 0
    || !Number.isSafeInteger(cancellation.pending_child_count) || cancellation.pending_child_count < 0
    || (cancellation.diagnostic !== null && typeof cancellation.diagnostic !== "string"))) throw new ApiError("取消进度格式无效", "invalid_activity", 200);
  if ((activity.event_sequence !== undefined && (!Number.isSafeInteger(activity.event_sequence) || activity.event_sequence < 0)) || (activity.steps !== undefined && (!Array.isArray(activity.steps) || !activity.steps.every((step) =>
    isRecord(step) && typeof step.id === "string" && Boolean(step.id) && Number.isInteger(step.sequence) && step.sequence >= 0 &&
    ["model", "tool"].includes(step.kind) && ["running", "completed", "failed", "cancelled", "approval_required", "interrupted", "uncertain"].includes(step.status) &&
    typeof step.started_at === "string" && typeof step.completed_at === "string" && (step.call_id === null || typeof step.call_id === "string")
  )))) throw new ApiError("运行步骤数据格式无效", "invalid_activity", 200);
  return activity;
}

export async function abandonRun(sessionId: string, runId: string): Promise<RunActivity> {
  return validateRunActivity(await requestJson<RunActivity>(`${API_ROOT}/sessions/${sessionId}/runs/${runId}/abandon`, { method: "POST" }));
}

export async function cancelSession(sessionId: string, runId: string): Promise<CancelReceipt> {
  return await requestJson<CancelReceipt>(
    `${API_ROOT}/sessions/${sessionId}/cancel`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, reason: "用户点击停止" }),
    },
  );
}

export async function watchRunActivity(sessionId: string, runId: string,
  onActivity: (activity: RunActivity) => void, signal: AbortSignal, cursor?: string): Promise<void> {
  await streamRequest(`${API_ROOT}/sessions/${sessionId}/runs/${encodeURIComponent(runId)}/events`, {
    headers: cursor ? { "Last-Event-ID": cursor } : {},
  }, (event) => {
    if (event.event !== "run.snapshot") return;
    const activity = validateRunActivity(event.data as unknown as RunActivity);
    if (activity.run_id !== runId || !activity.generation || !Number.isInteger(activity.control_revision)
      || event.id !== `e${activity.event_sequence}-c${activity.control_revision}`) throw new ApiError("运行观察快照格式无效", "invalid_activity", 200);
    onActivity(activity);
  }, signal);
}

export async function submitMessage(
  sessionId: string,
  content: string,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  await streamRequest(`${API_ROOT}/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  }, onEvent, signal);
}

export async function resolveApproval(
  approvalId: string,
  approved: boolean,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  await streamRequest(`${API_ROOT}/approvals/${approvalId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved }),
  }, onEvent, signal);
}

export async function resumeDelegation(
  childRunId: string,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  await streamRequest(`${API_ROOT}/delegations/${childRunId}/resume`, {
    method: "POST",
  }, onEvent, signal);
}

export async function resumeRun(
  runId: string,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  await streamRequest(`${API_ROOT}/runs/${runId}/resume`, {
    method: "POST",
  }, onEvent, signal);
}

async function streamRequest(
  url: string,
  init: RequestInit,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(url, { ...init, signal });
  if (!response.ok) {
    throw await responseError(response);
  }
  if (response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "text/event-stream") {
    throw new ApiError("服务器未返回正确的消息流，结果待确认", "invalid_stream_type", response.status);
  }
  if (response.body === null) {
    throw new ApiError("服务器未返回消息流", "missing_stream", response.status);
  }
  await readEventStream(response.body, onEvent);
}

export async function readEventStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  let finished = false;
  try {
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replaceAll("\r\n", "\n");
    let separator = buffer.indexOf("\n\n");
    while (separator >= 0) {
      const block = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const event = parseEventBlock(block);
      if (event !== null) {
        onEvent(event);
      }
      separator = buffer.indexOf("\n\n");
    }
    if (done) {
      finished = true;
      return;
    }
  }
  } finally {
    if (!finished) { try { await reader.cancel(); } catch { /* 断连后的流可能已取消。 */ } }
    reader.releaseLock();
  }
}

function parseEventBlock(block: string): StreamEvent | null {
  let eventName = "message";
  let id: string | undefined;
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("id:")) {
      id = line.slice(3).trimStart();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  const parsed: unknown = JSON.parse(dataLines.join("\n"));
  if (!isRecord(parsed)) {
    throw new ApiError("消息流数据格式无效", "invalid_stream", 200);
  }
  return { event: eventName, data: parsed, ...(id === undefined ? {} : { id }) };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
