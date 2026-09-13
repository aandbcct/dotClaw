import type {
  ErrorEnvelope,
  SessionDetail,
  SessionSummary,
  StreamEvent,
  RunActivity,
  WorkspaceOptions,
} from "./types";

const API_ROOT = "/api/v1";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
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
    );
  } catch {
    return new ApiError("请求失败，请稍后重试", "network_error", response.status);
  }
}

export function listSessions(): Promise<SessionSummary[]> {
  return requestJson<SessionSummary[]>(`${API_ROOT}/sessions`);
}

export function getSession(sessionId: string): Promise<SessionDetail> {
  return requestJson<SessionDetail>(`${API_ROOT}/sessions/${sessionId}`);
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
  if (!isRecord(activity) || !Array.isArray(activity.tools)) {
    throw new ApiError("运行状态数据格式无效", "invalid_activity", 200);
  }
  return activity;
}

export async function cancelSession(sessionId: string): Promise<void> {
  await requestJson<Record<string, unknown>>(
    `${API_ROOT}/sessions/${sessionId}/cancel`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "用户点击停止" }),
    },
  );
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

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
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
      return;
    }
  }
}

function parseEventBlock(block: string): StreamEvent | null {
  let eventName = "message";
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
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
  return { event: eventName, data: parsed };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
