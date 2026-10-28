import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiError, submitMessage } from "./api";
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
  const mock = setup((url) => url.endsWith("/messages") ? sse(event("run.failed", { run_id: "empty-run", error: emptyError })) : undefined);
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("button", { name: "当前会话更多操作" });
  await user.type(screen.getByLabelText("输入消息"), "原始问题");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(emptyError.message);
  expect(alert.textContent).toContain("empty-run");
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
