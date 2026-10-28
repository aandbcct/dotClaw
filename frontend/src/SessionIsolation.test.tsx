import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import type { SessionSummary } from "./types";

const a: SessionSummary = { id: "a", title: "会话 A", agent_id: "default", model: "chat-model", created_at: "2026-10-22T00:00:00Z", updated_at: "2026-10-22T00:00:00Z" };
const b = { ...a, id: "b", title: "会话 B" };
const encoder = new TextEncoder();
const block = (name: string, data: Record<string, unknown>) => encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
const sse = (body: BodyInit) => new Response(body, { headers: { "Content-Type": "text/event-stream" } });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

it("两会话流、错误、停止和完成回填相互隔离，不抢走当前焦点", async () => {
  const controllers = new Map<string, ReadableStreamDefaultController<Uint8Array>>();
  const cancelled: string[] = [];
  let aDone = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return json([a, b]);
    if (url.includes("/activity")) return json(null);
    if (url.endsWith("/messages")) {
      const id = url.includes("/a/") ? "a" : "b";
      return sse(new ReadableStream<Uint8Array>({ start(controller) { controllers.set(id, controller); } }));
    }
    if (url.endsWith("/cancel")) {
      cancelled.push(url);
      controllers.get("b")!.enqueue(block("run.cancelled", { run_id: "rb" }));
      controllers.get("b")!.close();
      return json({ run_id: "rb" });
    }
    if (url.endsWith("/a")) return json({ ...a, messages: aDone ? [{ id: "saved-a", run_ids: ["ra"], role: "assistant", content: "A 最终回答", created_at: a.updated_at }] : [] });
    return json({ ...b, messages: [] });
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("button", { name: "当前会话更多操作" });
  await user.type(screen.getByLabelText("输入消息"), "A 问题");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText("正在发送", { exact: true })).toBeTruthy();
  await act(async () => { controllers.get("a")!.enqueue(block("run.accepted", { session_id: "a", run_id: "ra" })); });
  expect(await screen.findByText("处理中", { exact: true })).toBeTruthy();
  await act(async () => { controllers.get("a")!.enqueue(block("message.delta", { run_id: "ra", content: "A 部分", kind: "response_delta" })); });
  await screen.findByText("A 部分");
  await user.click(screen.getByRole("button", { name: "会话 B" }));
  await user.type(screen.getByLabelText("输入消息"), "B 草稿");
  await act(async () => { controllers.get("a")!.enqueue(block("message.delta", { run_id: "ra", content: "继续", kind: "response_delta" })); });
  expect(screen.queryByText("A 部分继续")).toBeNull();
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("B 草稿");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  await act(async () => { controllers.get("b")!.enqueue(block("run.accepted", { session_id: "b", run_id: "rb" })); });
  expect(await screen.findByText("处理中", { exact: true })).toBeTruthy();
  expect(screen.queryByText("运行 ID：ra")).toBeNull();
  expect(screen.getByText("运行 ID：rb")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "停止运行" }));
  await waitFor(() => expect(cancelled).toEqual(["/api/v1/sessions/b/cancel"]));
  expect(screen.getByRole("button", { name: "会话 B" }).getAttribute("aria-current")).toBe("page");
  aDone = true;
  await act(async () => {
    controllers.get("a")!.enqueue(block("run.completed", { run_id: "ra", final_message: { content: "A 最终回答" } }));
    controllers.get("a")!.close();
  });
  expect(screen.queryByText("A 最终回答")).toBeNull();
  await user.click(screen.getByRole("button", { name: "会话 A" }));
  expect(await screen.findByText("A 最终回答")).toBeTruthy();
  expect(screen.queryByText("B 草稿")).toBeNull();
});

it("上翻阅读不跟随流更新，回到底部后继续跟随", async () => {
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return json([a]);
    if (url.includes("/activity")) return json(null);
    if (url.endsWith("/messages")) return sse(new ReadableStream<Uint8Array>({ start(value) { controller = value; } }));
    return json({ ...a, messages: [] });
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("button", { name: "当前会话更多操作" });
  await user.type(screen.getByLabelText("输入消息"), "长回答"); await user.click(screen.getByRole("button", { name: "发送消息" }));
  const region = screen.getByRole("region", { name: "对话内容" });
  Object.defineProperties(region, { scrollHeight: { configurable: true, value: 1200 }, clientHeight: { configurable: true, value: 400 }, scrollTop: { configurable: true, writable: true, value: 100 } });
  fireEvent.scroll(region);
  await act(async () => { controller!.enqueue(block("message.delta", { content: "新增内容", kind: "response_delta" })); });
  expect(region.scrollTop).toBe(100);
  await user.click(screen.getByRole("button", { name: /有新内容 · 回到底部/ }));
  expect(region.scrollTop).toBe(1200);
  await act(async () => { controller!.enqueue(block("run.cancelled", {})); controller!.close(); });
});

it("刷新后只轮询已知运行并回填最终结果，不重复发送", async () => {
  let done = false;
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return json([a]);
    if (url.includes("/activity")) return json({ run_id: "ra", status: done ? "completed" : "running", tools: [], approval_id: null, child_run_id: null });
    return json({ ...a, messages: done ? [{ id: "saved", role: "assistant", content: "后台最终结果", created_at: a.updated_at }] : [] });
  });
  vi.stubGlobal("fetch", mock); render(<App />);
  await screen.findByText("结果待确认", { exact: true });
  done = true;
  await screen.findByText("后台最终结果", {}, { timeout: 2500 });
  expect(mock.mock.calls.some(([input]) => String(input).endsWith("/messages"))).toBe(false);
});

it("待审批入口显式展示，展开参数后才能决定并继续消费恢复流", async () => {
  let resolved = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return json([a]);
    if (url.includes("/activity")) return json({ run_id: "ra", status: resolved ? "cancelled" : "waiting_approval", approval_id: "approve-a", child_run_id: null, tools: [{ call_id: "c", name: "write", status: "approval_required", arguments: { path: "demo.txt" }, result: "", error: "", started_at: "", completed_at: "" }] });
    if (url.includes("/approvals/")) { resolved = true; return sse(block("run.cancelled", { run_id: "ra" })); }
    return json({ ...a, messages: [] });
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("group", { name: "工具审批" });
  await user.click(screen.getByText("参数", { selector: "summary" }));
  expect(screen.getByText(/demo.txt/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "批准并继续" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "批准并继续" })).toBeNull());
  expect(resolved).toBe(true);
});

it("恢复检查点调用原运行接口，失败消息只显示在所属会话", async () => {
  let restored = false;
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); calls.push(url);
    if (url.endsWith("/sessions")) return json([a, b]);
    if (url.includes("/b/activity")) return json(null);
    if (url.includes("/activity")) return json({ run_id: "ra", status: restored ? "failed" : "recoverable", tools: [], approval_id: null, child_run_id: null });
    if (url.endsWith("/runs/ra/resume")) {
      restored = true;
      return sse(block("run.failed", { run_id: "ra", error: { message: "A 恢复失败" } }));
    }
    return json({ ...(url.endsWith("/b") ? b : a), messages: [] });
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("button", { name: "恢复运行" });
  await user.click(screen.getByRole("button", { name: "恢复运行" }));
  await screen.findByText("A 恢复失败");
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "会话 B" }));
  expect(screen.queryByText("A 恢复失败")).toBeNull();
  await user.type(screen.getByLabelText("输入消息"), "B 保留草稿");
  await user.click(screen.getByRole("button", { name: "会话 A" }));
  expect(await screen.findByText("A 恢复失败")).toBeTruthy();
  expect(calls.some((url) => url.endsWith("/messages"))).toBe(false);
});

it("终态历史尚未回填时保留下一条草稿，不静默吞掉提交", async () => {
  let reads = 0, sends = 0;
  let finishHistory: (response: Response) => void = () => undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return json([a]);
    if (url.includes("/activity")) return json(null);
    if (url.endsWith("/messages")) {
      sends++;
      return sse(block(sends === 1 ? "run.completed" : "run.cancelled", { run_id: "r" + sends, final_message: { content: "首轮完成" } }));
    }
    if (++reads === 2) return new Promise<Response>((resolve) => { finishHistory = resolve; });
    return json({ ...a, messages: [] });
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole("button", { name: "当前会话更多操作" });
  await user.type(screen.getByLabelText("输入消息"), "第一条");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("首轮完成");
  await user.type(screen.getByLabelText("输入消息"), "第二条保留");
  fireEvent.keyDown(screen.getByLabelText("输入消息"), { key: "Enter" });
  expect(sends).toBe(1);
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("第二条保留");
  await act(async () => { finishHistory(json({ ...a, messages: [] })); });
  await user.click(await screen.findByRole("button", { name: "发送消息" }));
  await waitFor(() => expect(sends).toBe(2));
});
