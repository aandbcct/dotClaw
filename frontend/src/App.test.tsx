import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { SessionSummary } from "./types";

const session: SessionSummary = { id: "session-1", title: "本地测试会话", agent_id: "default", model: "chat-model", created_at: "2026-09-01T09:00:00Z", updated_at: "2026-09-01T10:00:00Z" };
const options = { agents: [{ id: "default", name: "默认助手", description: "通用任务" }, { id: "writer", name: "写作助手", description: "内容写作" }], models: ["chat-model", "fast-model"], default_model: "chat-model", default_agent_id: "default" };
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });
function json(payload: unknown, status = 200): Response { return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }); }
function event(name: string, data: Record<string, unknown>): string { return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`; }
function stream(blocks: string[]): Response { return new Response(blocks.join(""), { headers: { "Content-Type": "text/event-stream" } }); }
function setup(handler?: (url: string, init?: RequestInit) => Response | Promise<Response> | undefined, sessions: SessionSummary[] = [session]) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const result = handler?.(url, init);
    if (result) return await result;
    if (url.endsWith("/options")) return json(options);
    if (url.includes("/activity")) return json(null);
    if (url.endsWith("/sessions")) return json(sessions);
    const found = sessions.find((item) => url.endsWith("/" + item.id)) || session;
    return json({ ...found, messages: [] });
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}
async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "当前会话更多操作" }));
}

describe("对话工作区", () => {
  it("首条输入只创建一次并提交一次，自动使用默认选项", async () => {
    const calls: string[] = [];
    setup((url, init) => {
      if (url.endsWith("/sessions") && init?.method === "POST") { calls.push("create"); return json(session, 201); }
      if (url.endsWith("/messages")) { calls.push("send"); return stream([event("run.cancelled", { run_id: "r1" })]); }
    }, []);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("有什么可以帮你？");
    await user.type(screen.getByLabelText("输入消息"), "第一条消息");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(calls).toEqual(["create", "send"]));
    expect(screen.getByText("第一条消息")).toBeTruthy();
  });

  it("创建失败保留输入和错误；无可用模型不给出虚假发送成功", async () => {
    setup((url) => url.endsWith("/options") ? json({ ...options, models: [] }) : undefined, []);
    const user = userEvent.setup(); render(<App />);
    await user.type(screen.getByLabelText("输入消息"), "保留草稿");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("保留草稿");
  });

  it("高级选择保留智能体和模型，选择时不创建空会话", async () => {
    let body: Record<string, unknown> | null = null;
    const mock = setup((url, init) => {
      if (url.endsWith("/sessions") && init?.method === "POST") { body = JSON.parse(String(init.body)); return json({ ...session, model: "fast-model", agent_id: "writer" }, 201); }
      if (url.endsWith("/messages")) return stream([event("run.cancelled", {})]);
    }, []);
    const user = userEvent.setup(); render(<App />);
    await user.click(screen.getByRole("button", { name: "智能体与模型" }));
    await user.selectOptions(screen.getByLabelText("Agent"), "writer");
    await user.selectOptions(screen.getByLabelText("模型"), "fast-model");
    await user.click(screen.getByRole("button", { name: "使用此选择" }));
    expect(mock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    await user.type(screen.getByLabelText("输入消息"), "写作");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(body).toEqual({ title: "新对话", agent_id: "writer", model: "fast-model" }));
  });

  it("切模型失败保留原模型和选择弹层，成功后更新摘要", async () => {
    let fail = true;
    setup((url, init) => {
      if (url.endsWith("/model") && init?.method === "PATCH") return fail ? json({ error: { code: "session_busy", message: "会话正在运行" } }, 409) : json({ ...session, model: "fast-model" });
    });
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole("button", { name: /chat-model/ }));
    await user.click(screen.getByRole("button", { name: "fast-model" }));
    expect(await screen.findByText("会话正在运行")).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "选择模型" })).toBeTruthy();
    fail = false;
    await user.click(screen.getByRole("button", { name: "fast-model" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "选择模型" })).toBeNull());
    expect(screen.getByRole("button", { name: /fast-model/ })).toBeTruthy();
  });

  it("删除失败保留列表，成功 204 不读取 JSON 并清理偏好", async () => {
    let fail = true;
    localStorage.setItem("dotclaw.pins.v1", JSON.stringify([session.id]));
    setup((url, init) => init?.method === "DELETE" ? fail ? json({ error: { code: "session_busy", message: "请处理运行" } }, 409) : new Response(null, { status: 204 }) : undefined);
    const user = userEvent.setup(); render(<App />);
    await openMenu(user); await user.click(screen.getByRole("button", { name: "删除会话" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    expect(await screen.findByText("请处理运行")).toBeTruthy();
    expect(screen.getByRole("button", { name: "本地测试会话" })).toBeTruthy();
    fail = false; await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "本地测试会话" })).toBeNull());
    expect(JSON.parse(localStorage.getItem("dotclaw.pins.v1") || "[]")).toEqual([]);
  });

  it("重命名、归档和恢复仍使用元数据接口", async () => {
    let current = { ...session };
    setup((url, init) => {
      if (init?.method === "PATCH") { current = { ...current, ...JSON.parse(String(init.body)) }; return json(current); }
    });
    const user = userEvent.setup(); render(<App />);
    await openMenu(user); await user.click(screen.getByRole("button", { name: "重命名" }));
    await user.clear(screen.getByLabelText("会话新标题")); await user.type(screen.getByLabelText("会话新标题"), "我的计划");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "我的计划" });
    await user.click(screen.getByRole("button", { name: "当前会话更多操作" }));
    await user.click(screen.getByRole("button", { name: "归档" }));
    await user.click(screen.getByRole("button", { name: "已归档" }));
    await user.click(screen.getByRole("button", { name: "更多操作：我的计划" }));
    await user.click(screen.getByRole("button", { name: "恢复会话" }));
    await screen.findByText("暂无归档会话");
    expect(current.archived).toBe(false);
  });

  it("输入法选词回车不发送，普通回车发送", async () => {
    const mock = setup((url) => url.endsWith("/messages") ? stream([event("run.cancelled", {})]) : undefined);
    render(<App />); await screen.findByRole("button", { name: "当前会话更多操作" });
    const input = screen.getByLabelText("输入消息");
    fireEvent.change(input, { target: { value: "中文输入" } });
    fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: "Enter" }); fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", isComposing: true }); fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(mock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(mock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1));
  });

  it("草稿按会话隔离，外观和最近会话在本机恢复", async () => {
    const second = { ...session, id: "session-2", title: "第二会话" };
    setup(undefined, [session, second]);
    const user = userEvent.setup(); const first = render(<App />);
    await screen.findByRole("button", { name: "当前会话更多操作" });
    await user.type(screen.getByLabelText("输入消息"), "第一份草稿");
    await user.click(screen.getByRole("button", { name: "第二会话" }));
    expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("");
    await user.type(screen.getByLabelText("输入消息"), "第二份草稿");
    await user.click(screen.getByRole("button", { name: "切换深色外观" })); first.unmount();
    render(<App />); await screen.findByRole("button", { name: "当前会话更多操作" });
    expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("第二份草稿");
    expect(screen.getByRole("button", { name: "切换浅色外观" })).toBeTruthy();
  });

  it("加载已保存消息并正确处理成功流的最终正文", async () => {
    let completed = false;
    setup((url) => {
      if (url.endsWith("/session-1")) return json({ ...session, messages: completed ? [{ id: "persisted", role: "assistant", content: "最终回答", run_ids: ["run-1"], created_at: session.updated_at }] : [] });
      if (url.endsWith("/messages")) { completed = true; return stream([event("message.delta", { kind: "response_delta", content: "最终" }), event("message.delta", { kind: "response_delta", content: "回答" }), event("run.completed", { run_id: "run-1", final_message: { content: "最终回答" } })]); }
    });
    const user = userEvent.setup(); render(<App />);
    await screen.findByRole("button", { name: "当前会话更多操作" });
    await user.type(screen.getByLabelText("输入消息"), "请回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(screen.getAllByText("最终回答")).toHaveLength(1));
  });
});

it("首次创建尚未完成时消息区立即显示发送提示，创建失败保留草稿", async () => {
  let rejectCreation: (reason: Error) => void = () => undefined;
  setup((url, init) => url.endsWith("/sessions") && init?.method === "POST" ? new Promise<Response>((_, reject) => { rejectCreation = reject; }) : undefined, []);
  const user = userEvent.setup(); render(<App />);
  await screen.findByText("有什么可以帮你？");
  await user.type(screen.getByLabelText("输入消息"), "创建期间的原问题");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  expect((await screen.findByText("正在发送")).closest("article")).toBeTruthy();
  expect(document.querySelector(".workspace-header [role=status]")).toBeNull();
  rejectCreation(new Error("隔离创建失败"));
  await screen.findByText("隔离创建失败");
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("创建期间的原问题");
});
