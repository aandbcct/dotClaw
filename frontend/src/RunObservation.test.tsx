import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useSessionWorkspace } from "./useSessionWorkspace";
import type { RunActivity, SessionSummary } from "./types";
import { readEventStream } from "./api";

const session: SessionSummary = { id: "a", title: "会话", agent_id: "default", model: "model", created_at: "", updated_at: "" };
const initial: RunActivity = { run_id: "run-a", status: "running", approval_id: null, child_run_id: null,
  tools: [], steps: [], event_sequence: 1, control_revision: 0, generation: "host-one", cancellation: null };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
function channel(signal: AbortSignal) {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  signal.addEventListener("abort", () => { try { controller.error(new DOMException("aborted", "AbortError")); } catch { /* 已关闭的观察无需重复释放。 */ } });
  return { response: new Response(body, { headers: { "Content-Type": "text/event-stream" } }), signal,
    event(name: string, data: Record<string, unknown>) { controller.enqueue(new TextEncoder().encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)); },
    push(activity: RunActivity) { controller.enqueue(new TextEncoder().encode(`id: e${activity.event_sequence}-c${activity.control_revision}\nevent: run.snapshot\ndata: ${JSON.stringify(activity)}\n\n`)); },
    close() { controller.close(); }, fail() { controller.error(new Error("断流")); } };
}
function setup() {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0.5);
  const channels: ReturnType<typeof channel>[] = [];
  let activity: RunActivity | null = initial;
  let nextActivity: Promise<Response> | undefined;
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return json([session, { ...session, id: "b" }]);
    if (url.endsWith("/events")) { const value = channel(init!.signal as AbortSignal); channels.push(value); return value.response; }
    if (url.includes("/activity")) {
      if (url.includes("/b/")) return json(null);
      if (nextActivity) { const value = nextActivity; nextActivity = undefined; return value; }
      return json(activity);
    }
    if (url.endsWith("/cancel")) return json({ run_id: "run-a", disposition: "accepted", control_revision: 1, request_id: "cancel-one", outcome: null, requested_at: "time" }, 202);
    return json({ ...session, id: url.endsWith("/b") ? "b" : "a", messages: [] });
  });
  vi.stubGlobal("fetch", fetcher);
  return { channels, fetcher, setActivity(value: RunActivity | null) { activity = value; }, deferActivity(value: Promise<Response>) { nextActivity = value; } };
}
async function flush() { await act(async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); }); }
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

it("共享解析器支持跨字节中文及分段CRLF，协议异常释放reader", async () => {
  const encoded = new TextEncoder().encode('id: e3-c2\r\nevent: run.snapshot\r\ndata: {"text":"中文"}\r\n\r\n');
  const stream = new ReadableStream<Uint8Array>({ start(controller) {
    for (const byte of encoded) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  } });
  const events: unknown[] = [];
  await readEventStream(stream, (event) => events.push(event));
  expect(events).toEqual([{ event: "run.snapshot", id: "e3-c2", data: { text: "中文" } }]);
  expect(stream.locked).toBe(false);
  let cancelled = false;
  const invalid = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode('event: run.snapshot\ndata: []\n\n')); }, cancel() { cancelled = true; } });
  await expect(readEventStream(invalid, () => undefined)).rejects.toThrow("消息流数据格式无效");
  expect(cancelled).toBe(true);
  expect(invalid.locked).toBe(false);
});

it("正常60秒只有一条状态流且零周期activity请求，202保持正在停止直到真实终态", async () => {
  const state = setup();
  const { result } = renderHook(useSessionWorkspace);
  await flush();
  expect(state.channels).toHaveLength(1);
  await act(async () => { state.channels[0].push(initial); });
  const queries = state.fetcher.mock.calls.filter(([url]) => String(url).includes("/activity")).length;
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(state.fetcher.mock.calls.filter(([url]) => String(url).includes("/activity"))).toHaveLength(queries);
  await act(async () => { await result.current.stop("a"); await result.current.stop("a"); });
  expect(result.current.views.a.phase).toBe("stopping");
  const cancellations = state.fetcher.mock.calls.filter(([url]) => String(url).endsWith("/cancel"));
  expect(cancellations).toHaveLength(1);
  expect(JSON.parse(cancellations[0][1]!.body as string).run_id).toBe("run-a");
  expect(state.channels[0].signal.aborted).toBe(false);
  await act(async () => { state.channels[0].push({ ...initial, status: "cancelled", event_sequence: 4, control_revision: 3 }); });
  expect(result.current.views.a.phase).toBe("cancelled");
  expect(state.channels[0].signal.aborted).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(state.channels).toHaveLength(1);
});

it.each([["cancelling", "stopping"], ["recoverable", "recoverable"]] as const)("run.suspended status=%s 映射为 %s 且保持状态观察", async (status, phase) => {
  const state = setup();
  const { result } = renderHook(useSessionWorkspace);
  await flush();

  await act(async () => { state.channels[0].event("run.suspended", { run_id: "run-a", status, error: { code: "control", message: "控制处理中" } }); });

  expect(result.current.views.a.phase).toBe(phase);
  expect(result.current.views.a.activity?.status).toBe(status);
  expect(state.channels[0].signal.aborted).toBe(false);
});

it("断流立即GET对账，按1/2/4/8秒重试，恢复后停止降级请求", async () => {
  const state = setup();
  renderHook(useSessionWorkspace);
  await flush();
  await act(async () => { state.channels[0].push(initial); state.channels[0].fail(); });
  await flush();
  expect(state.fetcher.mock.calls.filter(([url]) => String(url).includes("/activity"))).toHaveLength(2);
  for (const [index, delay] of [1000, 2000, 4000, 8000].entries()) {
    await act(async () => { await vi.advanceTimersByTimeAsync(delay - 1); });
    expect(state.channels).toHaveLength(index + 1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(state.channels).toHaveLength(index + 2);
    if (index < 3) { await act(async () => { state.channels.at(-1)!.fail(); }); await flush(); }
  }
  const latest = state.channels.at(-1)!;
  await act(async () => { latest.push({ ...initial, event_sequence: 2 }); });
  const queries = state.fetcher.mock.calls.filter(([url]) => String(url).includes("/activity")).length;
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(state.fetcher.mock.calls.filter(([url]) => String(url).includes("/activity"))).toHaveLength(queries);
});

it("双游标任一维倒退丢弃，Host换代更新基线，同游标初始快照可更新恢复状态", async () => {
  const state = setup();
  const { result } = renderHook(useSessionWorkspace);
  await flush();
  await act(async () => { state.channels[0].push({ ...initial, event_sequence: 3, control_revision: 2 }); });
  await act(async () => { state.channels[0].push({ ...initial, event_sequence: 4, control_revision: 1, status: "waiting_approval" }); });
  expect(result.current.views.a.phase).toBe("running");
  await act(async () => { state.channels[0].push({ ...initial, event_sequence: 2, control_revision: 3, status: "waiting_approval" }); });
  expect(result.current.views.a.activity?.event_sequence).toBe(3);
  await act(async () => { state.channels[0].push({ ...initial, generation: "host-two", event_sequence: 0, status: "recoverable" }); });
  expect(result.current.views.a.phase).toBe("recoverable");
  expect(result.current.views.a.activity?.generation).toBe("host-two");
  await act(async () => { state.channels[0].push({ ...initial, generation: "host-two", event_sequence: 0, status: "running" }); });
  expect(result.current.views.a.phase).toBe("running");
});

it("切换会话关闭观察与退避，旧连接迟到HTTP响应不能写入会话状态", async () => {
  const state = setup();
  const { result } = renderHook(useSessionWorkspace);
  await flush();
  let respond: (value: Response) => void = () => undefined;
  state.deferActivity(new Promise<Response>((resolve) => { respond = resolve; }));
  await act(async () => { state.channels[0].fail(); });
  await flush();
  await act(async () => { await result.current.open({ ...session, id: "b" }); });
  expect(state.channels[0].signal.aborted).toBe(true);
  await act(async () => { respond(json({ ...initial, status: "cancelling", control_revision: 9 })); });
  await flush();
  expect(result.current.views.a.phase).toBe("running");
  expect(result.current.views.a.activity?.control_revision).toBe(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  expect(state.channels).toHaveLength(1);
});

it("Host换代后的迟到旧HTTP结果不能把generation和游标改回去", async () => {
  const state = setup();
  const { result } = renderHook(useSessionWorkspace);
  await flush();
  let respond: (value: Response) => void = () => undefined;
  state.deferActivity(new Promise<Response>((resolve) => { respond = resolve; }));
  let query: Promise<void>;
  await act(async () => { query = result.current.checkRunStatus("a"); });
  await act(async () => { state.channels[0].push({ ...initial, generation: "host-two", event_sequence: 0, status: "recoverable" }); });
  await act(async () => { respond(json({ ...initial, event_sequence: 30 })); await query; });
  expect(result.current.views.a.activity?.generation).toBe("host-two");
  expect(result.current.views.a.phase).toBe("recoverable");
});

it("accepted前断流且第一次GET未找到运行时，退避对账能接管稍后创建的运行", async () => {
  const state = setup();
  state.setActivity(null);
  const { result } = renderHook(useSessionWorkspace);
  await flush();
  await act(async () => { await result.current.send("a", "测试接收前断线"); });
  await flush();
  expect(result.current.views.a.detached).toBe(true);
  expect(state.channels).toHaveLength(0);
  state.setActivity(initial);
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  await flush();
  expect(result.current.views.a.runId).toBe("run-a");
  expect(state.channels).toHaveLength(1);
});

it.each(["completed", "cancelled"])("状态流先确认%s，POST随后EOF或异常不能撤销终态", async (outcome) => {
  for (const failure of [false, true]) {
    const state = setup();
    state.setActivity(null);
    const original = state.fetcher.getMockImplementation()!;
    let body: ReturnType<typeof channel> | undefined;
    state.fetcher.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/messages")) { body = channel(init!.signal as AbortSignal); return body.response; }
      return await original(input, init);
    });
    const { result, unmount } = renderHook(useSessionWorkspace);
    await flush();
    let work: Promise<void>;
    await act(async () => { work = result.current.send("a", "终态竞争"); });
    await act(async () => { body!.event("run.accepted", { session_id: "a", run_id: "run-a" }); });
    await flush();
    await act(async () => { state.channels[0].push({ ...initial, status: outcome, event_sequence: 5 }); });
    await flush();
    expect(result.current.views.a.phase).toBe(outcome);
    await act(async () => { if (failure) body!.fail(); else body!.close(); await work; });
    expect(result.current.views.a.phase).toBe(outcome);
    expect(result.current.views.a.messages.at(-1)?.execution?.stage).toBe(outcome);
    unmount();
  }
});
