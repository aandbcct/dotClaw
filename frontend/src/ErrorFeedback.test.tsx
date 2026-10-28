import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiError, getRunActivity, resumeRun, submitMessage } from "./api";
import { useSessionWorkspace } from "./useSessionWorkspace";
import type { SessionSummary } from "./types";

const a: SessionSummary = { id: "a", title: "会话 A", model: "chat-model", agent_id: "default", created_at: "2026-10-28T00:00:00Z", updated_at: "2026-10-28T00:00:00Z" };
const b = { ...a, id: "b", title: "会话 B" };
const emptyError = { code: "empty_model_response", message: "模型未返回有效回答，请查看执行记录或稍后重新发送。", retryable: false };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const event = (name: string, data: Record<string, unknown>) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
const sse = (content: string) => new Response(content, { headers: { "Content-Type": "text/event-stream" } });
function setup(operation: (url: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const custom = operation(url, init); if (custom) return custom;
    if (url.endsWith("/sessions")) return json([a, b]);
    if (url.includes("/activity")) return json(null);
    return json({ ...(url.endsWith("/b") ? b : a), messages: [] });
  });
  vi.stubGlobal("fetch", mock); return mock;
}
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

it("空输出显示结构化错误、运行入口，恢复问题追加而不覆盖草稿", async () => {
  const mock = setup((url) => url.endsWith("/messages") ? sse(event("run.accepted", { session_id: "a", run_id: "empty-run" }) + event("run.failed", { run_id: "empty-run", error: emptyError })) : undefined);
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("button", { name: "当前会话更多操作" });
  await user.type(screen.getByLabelText("输入消息"), "原始问题");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(emptyError.message);
  await user.click(within(alert).getByRole("button", { name: "查看执行记录" }));
  expect(screen.getByText("运行 ID：empty-run")).toBeTruthy();
  expect(screen.queryByRole("dialog", { name: "执行详情" })).toBeNull();
  expect(within(alert).getByRole("button", { name: "查看执行记录" })).toBeTruthy();
  await user.type(screen.getByLabelText("输入消息"), "尚未发送的草稿");
  await user.click(screen.getByRole("button", { name: "恢复原问题到输入框" }));
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("尚未发送的草稿\n\n原始问题");
  expect(mock.mock.calls.filter(([url]) => String(url).endsWith("/messages"))).toHaveLength(1);
});

it.each([403, 409, 422])("明确 HTTP %i 拒绝结束发送态并保留错误", async (status) => {
  setup((url) => url.endsWith("/messages") ? json({ error: { code: "rejected", message: "明确拒绝请求", retryable: false } }, status) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { await result.current.send("a", "问题"); });
  expect(result.current.views.a).toMatchObject({ phase: "failed", detached: false, errorCode: "rejected", lastSubmittedText: "问题", retryable: false });
});

it.each(["network", "http500", "unknown-event", "protocol", "stream-error"])("%s 不能因 activity=null 被改写为确定失败", async (kind) => {
  const mock = setup((url) => {
    if (!url.endsWith("/messages")) return undefined;
    if (kind === "network") return Promise.reject(new TypeError("连接中断"));
    if (kind === "http500") return new Response("bad gateway", { status: 502 });
    if (kind === "protocol") return json({ message: "不是 SSE" });
    return sse(kind === "stream-error" ? event("stream.error", { error: { code: "internal_error", message: "流执行异常", retryable: false } }) : event("heartbeat", { ignored: true }));
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { await result.current.send("a", "保留的问题"); });
  expect(result.current.views.a).toMatchObject({ phase: "running", detached: true, retryable: false });
  expect(result.current.views.a.error).toContain("结果待确认");
  expect(mock.mock.calls.filter(([url]) => String(url).endsWith("/messages"))).toHaveLength(1);
});

it("查询只使用已知运行 ID，失败和恢复均不清除已确认模型错误", async () => {
  let queryFails = true;
  const mock = setup((url) => {
    if (url.endsWith("/messages")) return sse(event("run.failed", { run_id: "run-original", error: emptyError }));
    if (url.includes("run_id=run-original")) return queryFails ? Promise.reject(new Error("查询不可用")) : json({ run_id: "run-original", status: "failed", tools: [], error: emptyError });
    return undefined;
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { await result.current.send("a", "问题"); await result.current.checkRunStatus("a"); });
  expect(result.current.views.a.errorCode).toBe("empty_model_response");
  expect(result.current.views.a.statusError).toContain("状态查询失败");
  queryFails = false;
  await act(async () => { await result.current.checkRunStatus("a"); });
  expect(result.current.views.a.error).toBe(emptyError.message);
  expect(result.current.views.a.statusError).toBe("");
  expect(mock.mock.calls.filter(([url]) => String(url).includes("/activity")).map(([url]) => String(url)).slice(1).every((url) => url.includes("run_id=run-original"))).toBe(true);
});

it("只有指定运行确认为可恢复后才沿用恢复入口", async () => {
  let recoverable = false;
  setup((url) => {
    if (url.endsWith("/messages")) return sse(event("run.failed", { run_id: "retry-run", error: { ...emptyError, code: "service_unavailable", retryable: true } }));
    if (url.includes("run_id=retry-run")) return json(recoverable ? { run_id: "retry-run", status: "recoverable", tools: [] } : null);
    return undefined;
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { await result.current.send("a", "问题"); });
  expect(result.current.views.a.phase).toBe("failed");
  recoverable = true;
  await act(async () => { await result.current.checkRunStatus("a"); });
  expect(result.current.views.a.phase).toBe("recoverable");
});

it("未知连接结果后确认完成才回填历史并清除待确认提示", async () => {
  let completed = false;
  setup((url) => {
    if (url.endsWith("/messages")) return sse(event("message.delta", { run_id: "r", content: "部分正文" }));
    if (url.includes("run_id=r")) return json(completed ? { run_id: "r", status: "completed", tools: [] } : null);
    if (url.endsWith("/a")) return json({ ...a, messages: completed ? [{ id: "saved", role: "assistant", content: "最终正文", created_at: a.created_at, run_ids: ["r"] }] : [] });
    return undefined;
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { await result.current.send("a", "问题"); });
  expect(result.current.views.a.detached).toBe(true);
  completed = true;
  await act(async () => { await result.current.checkRunStatus("a"); });
  expect(result.current.views.a).toMatchObject({ phase: "completed", detached: false, error: "" });
  expect(result.current.views.a.messages[0].content).toBe("最终正文");
});

it("历史空答案明确提示，且不回写历史", async () => {
  const mock = setup((url) => url.endsWith("/a") ? json({ ...a, messages: [{ id: "old:assistant", role: "assistant", content: "", created_at: a.created_at }] }) : undefined);
  render(<App />);
  expect(await screen.findByText("该次回答为空")).toBeTruthy();
  expect(mock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
});

it("非 JSON HTTP 错误保留状态，SSE 类型不正确时不消费为正常响应", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>no</html>", { status: 503 })));
  await expect(submitMessage("a", "问题", () => undefined, new AbortController().signal)).rejects.toMatchObject({ status: 503, code: "http_error", retryable: false });
  vi.stubGlobal("fetch", vi.fn(async () => json({ ok: true })));
  await expect(submitMessage("a", "问题", () => undefined, new AbortController().signal)).rejects.toBeInstanceOf(ApiError);
});

it("其他运行的查询结果不能覆盖当前失败和运行 ID", async () => {
  setup((url) => {
    if (url.endsWith("/messages")) return sse(event("run.failed", { run_id: "original", error: emptyError }));
    if (url.includes("run_id=original")) return json({ run_id: "other", status: "completed", tools: [] });
    return undefined;
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { await result.current.send("a", "问题"); await result.current.checkRunStatus("a"); });
  expect(result.current.views.a).toMatchObject({ phase: "failed", runId: "original", errorCode: "empty_model_response" });
  expect(result.current.views.a.statusError).toContain("不一致");
});

it("HTTP 错误的可恢复标识来自响应而非状态码猜测", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ error: { code: "service_unavailable", message: "服务暂不可用", retryable: true } }, 503)));
  await expect(submitMessage("a", "问题", () => undefined, new AbortController().signal)).rejects.toMatchObject({ status: 503, retryable: true });
});

function controlledResponse() {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  return {
    response: () => new Response(stream, { headers: { "Content-Type": "text/event-stream" } }),
    push: (name: string, data: Record<string, unknown>) => controller.enqueue(new TextEncoder().encode(event(name, data))),
    close: () => controller.close(),
  };
}

it("首个模型增量前已经接收确认，确认不是终态且重复确认不降级阶段", async () => {
  const stream = controlledResponse();
  setup((url) => url.endsWith("/messages") ? stream.response() : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let finished = false;
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题").then(() => { finished = true; }); });
  expect(result.current.views.a.submissionStage).toBe("sending");
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "accepted-r" }); });
  expect(result.current.views.a).toMatchObject({ phase: "running", submissionStage: "accepted", runId: "accepted-r" });
  expect(finished).toBe(false);
  expect(result.current.views.a.messages.at(-1)?.content).toBe("");
  await act(async () => { stream.push("message.delta", { run_id: "accepted-r", kind: "response_delta", content: "" }); });
  expect(result.current.views.a.submissionStage).toBe("accepted");
  await act(async () => { stream.push("message.delta", { run_id: "accepted-r", kind: "reasoning_delta", content: "思考" }); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "accepted-r" }); });
  expect(result.current.views.a.submissionStage).toBe("streaming");
  await act(async () => { stream.push("run.cancelled", { run_id: "accepted-r" }); stream.close(); await sending; });
  expect(result.current.views.a.submissionStage).toBe("idle");
});

it.each([
  { session_id: "b", run_id: "other" },
  { session_id: "a", run_id: "other" },
  { session_id: "a", run_id: " " },
  { session_id: "a", run_id: 123 },
  { run_id: "other" },
])("错误确认不能覆盖正确 ID：%j", async (badData) => {
  const stream = controlledResponse();
  setup((url) => url.endsWith("/messages") ? stream.response() : url.includes("run_id=correct") ? json({ run_id: "correct", status: "running", tools: [] }) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "correct" }); stream.push("run.accepted", badData); });
  expect(result.current.views.a).toMatchObject({ runId: "correct", detached: true, errorCode: "invalid_run_accepted", submissionStage: "idle" });
  expect(result.current.views.b).toBeUndefined();
  await act(async () => { await result.current.checkRunStatus("a"); });
  expect(result.current.views.a.detached).toBe(true);
  await act(async () => { stream.push("run.completed", { run_id: "other", final_message: { content: "错误运行正文" } }); });
  expect(result.current.views.a.runId).toBe("correct");
  await act(async () => { stream.close(); await sending; });
  expect(result.current.views.a.runId).toBe("correct");
  expect(result.current.views.a.detached).toBe(true);
});

it("确认后断流按确认 ID 查询，查询失败和恢复不触发第二次发送", async () => {
  const stream = controlledResponse();
  let queryFailed = true;
  const mock = setup((url) => {
    if (url.endsWith("/messages")) return stream.response();
    if (url.includes("run_id=accepted-r")) return queryFailed ? Promise.reject(new Error("查询中断")) : json({ run_id: "accepted-r", status: "failed", tools: [], error: emptyError });
    return undefined;
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "accepted-r" }); stream.close(); await sending; });
  expect(result.current.views.a).toMatchObject({ detached: true, runId: "accepted-r", submissionStage: "idle" });
  expect(result.current.views.a.statusError).toContain("状态查询失败");
  queryFailed = false;
  await act(async () => { await result.current.checkRunStatus("a"); });
  expect(result.current.views.a).toMatchObject({ phase: "failed", detached: false, errorCode: "empty_model_response", runId: "accepted-r" });
  expect(mock.mock.calls.filter(([url]) => String(url).endsWith("/messages"))).toHaveLength(1);
});

it("旧服务可直接从发送进入生成，空增量不能提前进入生成阶段", async () => {
  const stream = controlledResponse();
  setup((url) => url.endsWith("/messages") ? stream.response() : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("message.delta", { run_id: "legacy", content: "", kind: "response_delta" }); });
  expect(result.current.views.a.submissionStage).toBe("sending");
  await act(async () => { stream.push("message.delta", { run_id: "legacy", content: "旧服务正文", kind: "response_delta" }); });
  expect(result.current.views.a.submissionStage).toBe("streaming");
  await act(async () => { stream.push("run.cancelled", { run_id: "legacy" }); stream.close(); await sending; });
});

const modelStep = (sequence: number, status = "running") => ({ id: `r:${sequence}`, sequence, kind: "model", status, started_at: "t", completed_at: "", call_id: null });

it("完成刷新按运行合并配对，保留思考、过程和错误，连续相同提问仍是独立消息", async () => {
  const stream = controlledResponse();
  let saved = false;
  setup((url) => {
    if (url.endsWith("/messages")) return stream.response();
    if (url.endsWith("/a")) return json({ ...a, messages: saved ? [
      { id: "persisted", role: "user", content: "同一个问题", created_at: a.created_at },
      { id: "persisted:assistant", role: "assistant", content: "正文", run_ids: ["r"], created_at: a.created_at },
    ] : [] });
    if (url.includes("run_id=r")) return json({ run_id: "r", status: saved ? "completed" : "running", event_sequence: 2, steps: [modelStep(1, "completed")], tools: [] });
    return undefined;
  });
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "同一个问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); stream.push("message.delta", { run_id: "r", kind: "reasoning_delta", content: "真实思考" }); await result.current.checkRunStatus("a"); });
  const original = result.current.views.a.activeMessageId;
  await act(async () => { stream.push("message.delta", { run_id: "r", content: "正文" }); saved = true; stream.push("run.completed", { run_id: "r" }); stream.close(); await sending; await result.current.refresh("a"); });
  expect(result.current.views.a.messages).toHaveLength(2);
  expect(result.current.views.a.activeMessageId).toBe("persisted:assistant");
  expect(result.current.views.a.messages[1]).toMatchObject({ reasoning: "真实思考", execution: { stage: "completed" } });
  expect(result.current.views.a.activitiesByRunId.r.steps).toHaveLength(1);
  expect(original).not.toBe(result.current.views.a.activeMessageId);
  const next = controlledResponse();
  setup((url) => url.endsWith("/messages") ? next.response() : url.endsWith("/a") ? json({ ...a, messages: [] }) : undefined);
  await act(async () => { sending = result.current.send("a", "同一个问题"); });
  await act(async () => { next.push("run.accepted", { session_id: "a", run_id: "r2" }); next.push("run.failed", { run_id: "r2", error: emptyError }); next.close(); await sending; });
  expect(result.current.views.a.messages).toHaveLength(4);
  expect(result.current.views.a.messages[1].run_ids).toEqual(["r"]);
  expect(result.current.views.a.messages[3]).toMatchObject({ run_ids: ["r2"], execution: { stage: "failed", error: emptyError } });
});

it("迟到查询不回退新正文，旧事件序号不覆盖步骤，同序号可更新恢复状态", async () => {
  const stream = controlledResponse();
  let response: (value: Response) => void = () => undefined;
  let pending = false;
  let activity = { run_id: "r", status: "running", tools: [], steps: [modelStep(1)], event_sequence: 1 };
  setup((url) => url.endsWith("/messages") ? stream.response() : url.includes("run_id=r") ? pending ? new Promise<Response>((resolve) => { response = resolve; }) : json(activity) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>, query: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); });
  await act(async () => { await result.current.checkRunStatus("a"); });
  pending = true;
  await act(async () => { query = result.current.checkRunStatus("a"); });
  await act(async () => { stream.push("message.delta", { run_id: "r", content: "新正文" }); });
  activity = { ...activity, steps: [modelStep(1, "completed"), modelStep(4)], event_sequence: 4 };
  await act(async () => { response(json(activity)); await query; });
  expect(result.current.views.a.messages.at(-1)?.execution?.stage).toBe("responding");
  pending = false;
  activity = { ...activity, steps: [modelStep(1)], event_sequence: 1 };
  await act(async () => { await result.current.checkRunStatus("a"); });
  expect(result.current.views.a.activitiesByRunId.r.event_sequence).toBe(4);
  activity = { ...activity, steps: [modelStep(4)], event_sequence: 4, status: "recoverable" };
  await act(async () => { stream.close(); await sending; });
  expect(result.current.views.a.messages.at(-1)?.execution?.stage).toBe("recoverable");
});

it("历史运行查询及查询失败只更新所属消息，不影响当前运行和错误", async () => {
  const stream = controlledResponse();
  let historyFails = false;
  setup((url) => url.endsWith("/messages") ? stream.response() : url.endsWith("/a") ? json({ ...a, messages: [{ id: "old", role: "assistant", content: "旧回答", run_ids: ["old-run"], created_at: a.created_at }] }) : url.includes("run_id=old-run") ? historyFails ? Promise.reject(new Error("历史查询失败")) : json({ run_id: "old-run", status: "completed", tools: [], steps: [], event_sequence: 2 }) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "新问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); await result.current.loadRunActivity("a", "old-run"); });
  const activeId = result.current.views.a.activeMessageId;
  historyFails = true;
  await act(async () => { await result.current.checkRunStatus("a", "old-run"); });
  expect(result.current.views.a).toMatchObject({ runId: "r", activeMessageId: activeId, phase: "running", statusError: "" });
  expect(result.current.views.a.messages[0].execution?.statusError).toContain("历史查询失败");
  await act(async () => { stream.push("run.cancelled", { run_id: "r" }); stream.close(); await sending; });
});

it("恢复复用原助手消息，保留旧思考但等待新增量才显示思考阶段", async () => {
  const stream = controlledResponse();
  let recoverable = false;
  setup((url) => url.endsWith("/messages") ? stream.response() : url.includes("run_id=r") ? json({ run_id: "r", status: recoverable ? "recoverable" : "running", tools: [], event_sequence: 2 }) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); stream.push("message.delta", { run_id: "r", kind: "reasoning_delta", content: "之前的思考" }); recoverable = true; stream.close(); await sending; });
  const original = result.current.views.a.activeMessageId!;
  const next = controlledResponse();
  let resumed: Promise<void>;
  await act(async () => { resumed = result.current.control("a", original, "r", (callback, signal) => { vi.stubGlobal("fetch", vi.fn(async () => next.response())); return resumeRun("r", callback, signal); }); });
  expect(result.current.views.a.messages).toHaveLength(2);
  expect(result.current.views.a.messages[1]).toMatchObject({ id: original, reasoning: "之前的思考", execution: { stage: "processing" } });
  await act(async () => { next.push("run.cancelled", { run_id: "r" }); next.close(); await resumed; });
  expect(result.current.views.a.activeMessageId).toBe(original);
});

it.each([{ event_sequence: -1 }, { event_sequence: "2" }, { steps: [{}] }, { steps: [{ ...modelStep(1), kind: "invented" }] }])("活动新增字段格式错误显示查询错误：%j", async (invalid) => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ run_id: "r", status: "running", tools: [], ...invalid })));
  await expect(getRunActivity("a", "r")).rejects.toMatchObject({ code: "invalid_activity" });
});

it("停止失败恢复原阶段，但迟到停止错误不能撤销终态", async () => {
  const stream = controlledResponse();
  let rejectStop: (reason: Error) => void = () => undefined;
  setup((url) => url.endsWith("/messages") ? stream.response() : url.endsWith("/cancel") ? new Promise<Response>((_, reject) => { rejectStop = reject; }) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>, stopping: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); stream.push("message.delta", { run_id: "r", kind: "reasoning_delta", content: "真实思考" }); });
  await act(async () => { stopping = result.current.stop("a"); });
  expect(result.current.views.a.messages.at(-1)?.execution?.stage).toBe("stopping");
  await act(async () => { rejectStop(new Error("停止连接错误")); await stopping; });
  expect(result.current.views.a.messages.at(-1)?.execution?.stage).toBe("thinking");
  await act(async () => { stopping = result.current.stop("a"); });
  await act(async () => { stream.push("run.cancelled", { run_id: "r" }); stream.close(); await sending; rejectStop(new Error("迟到错误")); await stopping; });
  expect(result.current.views.a.messages.at(-1)?.execution).toMatchObject({ stage: "cancelled", statusError: expect.stringContaining("迟到错误") });
});

it("迟到运行中查询不能撤销 SSE 审批，原运行的审批 ID 和入口保留", async () => {
  const stream = controlledResponse();
  let response: (value: Response) => void = () => undefined;
  let pending = true;
  setup((url) => url.endsWith("/messages") ? stream.response() : url.includes("run_id=r") ? pending ? new Promise<Response>((resolve) => { response = resolve; }) : json(null) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>, query: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); });
  await act(async () => { query = result.current.checkRunStatus("a"); });
  await act(async () => { stream.push("run.suspended", { run_id: "r", status: "waiting_approval", approval_id: "approval-r" }); });
  await act(async () => { response(json({ run_id: "r", status: "running", tools: [], event_sequence: 0 })); await query; });
  expect(result.current.views.a).toMatchObject({ phase: "suspended", activeMessageId: result.current.views.a.messages[1].id });
  expect(result.current.views.a.activitiesByRunId.r).toMatchObject({ status: "waiting_approval", approval_id: "approval-r" });
  expect(result.current.views.a.messages[1].execution?.stage).toBe("waiting_approval");
  pending = false;
  await act(async () => { stream.close(); await sending; });
});

it.each(["completed", "failed"])("迟到快照不能回退已确认 %s，未闭合步骤不伪造成功", async (outcome) => {
  const stream = controlledResponse();
  let resolveActivity: (value: Response) => void = () => undefined;
  const pendingActivity = new Promise<Response>((resolve) => { resolveActivity = resolve; });
  setup((url) => url.endsWith("/messages") ? stream.response() : url.includes("run_id=r") ? pendingActivity.then((response) => response.clone()) : undefined);
  const { result } = renderHook(useSessionWorkspace);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let sending: Promise<void>, query: Promise<void>;
  await act(async () => { sending = result.current.send("a", "问题"); });
  await act(async () => { stream.push("run.accepted", { session_id: "a", run_id: "r" }); stream.push("message.delta", { run_id: "r", kind: "reasoning_delta", content: "真实思考" }); });
  await act(async () => { query = result.current.checkRunStatus("a"); });
  await act(async () => { stream.push(`run.${outcome}`, { run_id: "r", error: outcome === "failed" ? emptyError : undefined, final_message: { content: "正文" } }); resolveActivity(json({ run_id: "r", status: "running", tools: [], steps: [modelStep(1)], event_sequence: 1 })); await query; stream.close(); await sending; });
  expect(result.current.views.a.phase).toBe(outcome);
  expect(result.current.views.a.activitiesByRunId.r.steps?.[0].status).toBe(outcome === "completed" ? "interrupted" : "failed");
  expect(result.current.views.a.messages[1]).toMatchObject({ reasoning: "真实思考", execution: { stage: outcome } });
  if (outcome === "failed") expect(result.current.views.a.messages[1].execution?.error).toEqual(emptyError);
});
