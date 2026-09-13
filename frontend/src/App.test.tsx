import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { SessionDetail, SessionSummary } from "./types";

const session: SessionSummary = {
  id: "session-1",
  title: "本地测试会话",
  agent_id: "default",
  model: "chat-model",
  created_at: "2026-09-01T09:00:00+08:00",
  updated_at: "2026-09-01T10:00:00+08:00",
};

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("dotClaw GUI", () => {
  it("新建会话先选择 Agent 和模型，并把选择提交给后端", async () => {
    let createBody: Record<string, unknown> | null = null;
    const created = { ...session, agent_id: "writer", model: "fast-model" };
    stubFetch((url, init) => {
      if (url.endsWith("/options")) return jsonResponse({
        agents: [
          { id: "default", name: "默认助手", description: "通用任务" },
          { id: "writer", name: "写作助手", description: "内容写作" },
        ],
        models: ["chat-model", "fast-model"],
        default_agent_id: "default",
        default_model: "chat-model",
      });
      if (url.endsWith("/sessions") && init?.method === "POST") {
        createBody = JSON.parse(String(init.body));
        return jsonResponse(created, 201);
      }
      if (url.endsWith("/activity")) return jsonResponse(null);
      if (url.endsWith("/session-1")) return jsonResponse({ ...created, messages: [] });
      return jsonResponse([]);
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("当前还没有会话");
    await user.click(screen.getByRole("button", { name: "创建第一个会话" }));
    await screen.findByRole("dialog", { name: "选择这次对话的工作方式" });
    await user.selectOptions(screen.getByLabelText("Agent"), "writer");
    await user.selectOptions(screen.getByLabelText("模型"), "fast-model");
    await user.click(screen.getByRole("button", { name: "开始对话" }));
    await screen.findByText("把任务交给 dotClaw。");
    expect(createBody).toEqual({ title: "新对话", agent_id: "writer", model: "fast-model" });
    expect(screen.getByText(/writer/)).toBeTruthy();
    expect(screen.getAllByText(/fast-model/).length).toBeGreaterThan(0);
  });

  it("展示真实工具过程，并可在网页批准后继续消费运行流", async () => {
    const approvalRequests: RequestInit[] = [];
    stubFetch((url, init) => {
      if (url.endsWith("/approvals/approval-1")) {
        approvalRequests.push(init ?? {});
        return eventStreamResponse([eventBlock("run.cancelled", {
          session_id: session.id,
          run_id: "run-1",
          status: "cancelled",
        })]);
      }
      if (url.includes("/activity")) return jsonResponse({
        run_id: "run-1",
        status: "waiting_approval",
        approval_id: "approval-1",
        child_run_id: null,
        tools: [{
          call_id: "call-1",
          name: "read_file",
          status: "approval_required",
          started_at: session.created_at,
          completed_at: session.updated_at,
          arguments: { path: "README.md" },
          result: "读取完成",
          error: "",
        }],
      });
      if (url.endsWith("/session-1")) return jsonResponse({ ...session, messages: [] });
      return jsonResponse([session]);
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("执行记录");
    await user.click(screen.getByText("read_file"));
    expect(screen.getByText(/README\.md/)).toBeTruthy();
    expect(screen.getByText("读取完成")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "批准并继续" }));
    await screen.findByText("已停止");
    expect(approvalRequests).toHaveLength(1);
    expect(JSON.parse(String(approvalRequests[0].body))).toEqual({ approved: true });
  });

  it("刷新后发现未结束运行时可以从网页恢复", async () => {
    let resumed = false;
    stubFetch((url, init) => {
      if (url.endsWith("/runs/run-1/resume") && init?.method === "POST") {
        resumed = true;
        return eventStreamResponse([eventBlock("run.cancelled", {
          session_id: session.id,
          run_id: "run-1",
          status: "cancelled",
        })]);
      }
      if (url.includes("/activity")) return jsonResponse({
        run_id: "run-1",
        status: "recoverable",
        approval_id: null,
        child_run_id: null,
        tools: [],
      });
      if (url.endsWith("/session-1")) return jsonResponse({ ...session, messages: [] });
      return jsonResponse([session]);
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "恢复运行" }));
    await screen.findByText("已停止");
    expect(resumed).toBe(true);
  });

  it("草稿按会话隔离，刷新后恢复且发送后清除", async () => {
    const second = { ...session, id: "session-2", title: "第二会话" };
    stubFetch((url, init) => {
      if (init?.method === "POST") return eventStreamResponse([eventBlock("run.cancelled", {})]);
      if (url.endsWith("/session-1")) return jsonResponse({ ...session, messages: [] });
      if (url.endsWith("/session-2")) return jsonResponse({ ...second, messages: [] });
      return jsonResponse([session, second]);
    });
    const user = userEvent.setup();
    const firstPage = render(<App />);
    await screen.findByText("把任务交给 dotClaw。");
    await user.type(screen.getByLabelText("输入消息"), "第一份草稿");
    await user.click(screen.getByRole("button", { name: /第二会话\s*chat-model/ }));
    expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("");
    await user.type(screen.getByLabelText("输入消息"), "第二份草稿");
    await user.click(screen.getByRole("button", { name: /本地测试会话\s*chat-model/ }));
    expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("第一份草稿");
    firstPage.unmount();
    render(<App />);
    await screen.findByText("把任务交给 dotClaw。");
    expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("第一份草稿");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("已停止");
    expect(JSON.parse(localStorage.getItem("dotclaw.drafts") || "{}")).toEqual({ "session-2": "第二份草稿" });
  });

  it("中文选词回车不发送，Shift Enter 换行，普通回车发送", async () => {
    const fetchMock = stubFetch((url, init) => {
      if (init?.method === "POST") return eventStreamResponse([eventBlock("run.cancelled", {})]);
      return jsonResponse(url.endsWith("/session-1") ? { ...session, messages: [] } : [session]);
    });
    render(<App />);
    await screen.findByText("把任务交给 dotClaw。");
    const input = screen.getByLabelText("输入消息");
    fireEvent.change(input, { target: { value: "中文输入" } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", shiftKey: true });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    await screen.findByText("已停止");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("搜索标题、重命名、归档和恢复使用持久化更新接口", async () => {
    let current = { ...session };
    stubFetch((url, init) => {
      if (init?.method === "PATCH") {
        current = { ...current, ...JSON.parse(String(init.body)) };
        return jsonResponse(current);
      }
      return jsonResponse(url.endsWith("/session-1") ? { ...current, messages: [] } : [current]);
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("把任务交给 dotClaw。");
    await user.type(screen.getByLabelText("搜索会话标题"), "不匹配");
    expect(screen.getByText("没有匹配的会话")).toBeTruthy();
    await user.clear(screen.getByLabelText("搜索会话标题"));
    await user.click(screen.getByRole("button", { name: "重命名会话：本地测试会话" }));
    await user.clear(screen.getByLabelText("会话新标题"));
    await user.type(screen.getByLabelText("会话新标题"), "我的计划");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("heading", { name: "我的计划" });
    await user.click(screen.getByRole("button", { name: "归档会话：我的计划" }));
    await screen.findByText("暂无会话");
    expect(current.archived).toBe(true);
    await user.click(screen.getByRole("button", { name: "已归档" }));
    await user.click(screen.getByRole("button", { name: "恢复会话：我的计划" }));
    await screen.findByText("暂无归档会话");
    expect(current.archived).toBe(false);
    await user.click(screen.getByRole("button", { name: "最近会话" }));
    expect(screen.getByRole("button", { name: /我的计划\s*chat-model/ })).toBeTruthy();
  });

  it("向上阅读时流式更新不拉走位置，回到底部后继续跟随", async () => {
    let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
    const encoder = new TextEncoder();
    stubFetch((url, init) => {
      if (init?.method === "POST") return Promise.resolve(new Response(new ReadableStream<Uint8Array>({ start(value) { controller = value; } })));
      return jsonResponse(url.endsWith("/session-1") ? { ...session, messages: [] } : [session]);
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("把任务交给 dotClaw。");
    await user.type(screen.getByLabelText("输入消息"), "长回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    const region = screen.getByRole("region", { name: "对话内容" });
    Object.defineProperties(region, { scrollHeight: { configurable: true, value: 1200 }, clientHeight: { configurable: true, value: 400 }, scrollTop: { configurable: true, writable: true, value: 100 } });
    fireEvent.scroll(region);
    controller?.enqueue(encoder.encode(eventBlock("message.delta", { kind: "response_delta", content: "新增内容" })));
    await screen.findByText("新增内容");
    expect(region.scrollTop).toBe(100);
    await user.click(screen.getByRole("button", { name: /有新内容 · 回到底部/ }));
    expect(region.scrollTop).toBe(1200);
    Object.defineProperty(region, "scrollHeight", { value: 1400 });
    controller?.enqueue(encoder.encode(eventBlock("message.delta", { kind: "response_delta", content: "继续输出" })));
    await screen.findByText("新增内容继续输出");
    expect(region.scrollTop).toBe(1400);
    controller?.enqueue(encoder.encode(eventBlock("run.cancelled", {})));
    controller?.close();
    await screen.findByText("已停止");
  });

  it("切换外观后重新打开页面仍保留选择", async () => {
    stubFetch(() => jsonResponse([]));
    const user = userEvent.setup();
    const firstPage = render(<App />);
    await screen.findByText("当前还没有会话");
    await user.click(screen.getByRole("button", { name: "切换深色外观" }));
    firstPage.unmount();

    render(<App />);
    await screen.findByText("当前还没有会话");
    expect(screen.getByRole("button", { name: "切换浅色外观" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "切换浅色外观" }));
    expect(localStorage.getItem("dotclaw.theme")).toBe("light");
  });

  it("加载会话并展示持久化历史", async () => {
    const detail: SessionDetail = {
      ...session,
      messages: [
        {
          id: "message-user",
          role: "user",
          content: "此前的问题",
          created_at: session.created_at,
        },
        {
          id: "message-assistant",
          role: "assistant",
          content: "已保存回答",
          created_at: session.updated_at,
        },
      ],
    };
    stubFetch((url) => {
      if (url.endsWith("/sessions/session-1")) {
        return jsonResponse(detail);
      }
      return jsonResponse([session]);
    });

    render(<App />);

    expect(await screen.findByText("此前的问题")).toBeTruthy();
    expect(screen.getByText("已保存回答")).toBeTruthy();
    expect(screen.getByText(/default/)).toBeTruthy();
  });

  it("消费 POST SSE 并且不重复追加成功终态中的完整回答", async () => {
    let detailRequests = 0;
    const completedDetail: SessionDetail = {
      ...session,
      messages: [
        {
          id: "persisted-user",
          role: "user",
          content: "请回答",
          created_at: session.created_at,
        },
        {
          id: "persisted-assistant",
          role: "assistant",
          content: "最终回答",
          created_at: session.updated_at,
        },
      ],
    };
    stubFetch((url, init) => {
      if (url.endsWith("/messages") && init?.method === "POST") {
        return eventStreamResponse([
          eventBlock("message.delta", {
            session_id: session.id,
            run_id: "run-1",
            kind: "reasoning_delta",
            content: "先分析",
          }),
          eventBlock("message.delta", {
            session_id: session.id,
            run_id: "run-1",
            kind: "response_delta",
            content: "最终",
          }),
          eventBlock("message.delta", {
            session_id: session.id,
            run_id: "run-1",
            kind: "response_delta",
            content: "回答",
          }),
          eventBlock("run.completed", {
            session_id: session.id,
            run_id: "run-1",
            status: "completed",
            final_message: {
              id: "answer-1",
              role: "assistant",
              content: "最终回答",
              created_at: session.updated_at,
            },
            has_streamed_response: true,
          }),
        ]);
      }
      if (url.endsWith("/sessions/session-1")) {
        detailRequests += 1;
        return jsonResponse(
          detailRequests === 1 ? { ...session, messages: [] } : completedDetail,
        );
      }
      return jsonResponse([session]);
    });

    const user = userEvent.setup();
    render(<App />);
    const input = await screen.findByLabelText("输入消息");
    await user.type(input, "请回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await screen.findByText("已完成");
    await waitFor(() => {
      expect(screen.getAllByText("最终回答")).toHaveLength(1);
    });
  });

  it("运行期间通过取消接口显式停止，而不是直接断开消息流", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
    const encoder = new TextEncoder();
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith("/messages") && init?.method === "POST") {
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
            controller.enqueue(
              encoder.encode(
                eventBlock("message.delta", {
                  session_id: session.id,
                  run_id: "run-1",
                  kind: "response_delta",
                  content: "处理中",
                }),
              ),
            );
          },
        });
        return Promise.resolve(
          new Response(stream, {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          }),
        );
      }
      if (url.endsWith("/cancel") && init?.method === "POST") {
        streamController?.enqueue(
          encoder.encode(
            eventBlock("run.cancelled", {
              session_id: session.id,
              run_id: "run-1",
              status: "cancelled",
            }),
          ),
        );
        streamController?.close();
        return jsonResponse({
          session_id: session.id,
          run_id: "run-1",
          status: "cancelling",
        });
      }
      if (url.endsWith("/sessions/session-1")) {
        return jsonResponse({ ...session, messages: [] });
      }
      return jsonResponse([session]);
    });

    const user = userEvent.setup();
    render(<App />);
    const input = await screen.findByLabelText("输入消息");
    await user.type(input, "执行长任务");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await user.click(await screen.findByRole("button", { name: "停止运行" }));

    expect(await screen.findByText("已停止")).toBeTruthy();
    expect(
      fetchMock.mock.calls.some(
        ([inputValue, init]) =>
          String(inputValue).endsWith("/cancel") && init?.method === "POST",
      ),
    ).toBe(true);
  });
});

function stubFetch(
  handler: (url: string, init?: RequestInit) => Promise<Response>,
) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(payload: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function eventStreamResponse(blocks: string[]): Promise<Response> {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const block of blocks) {
        controller.enqueue(encoder.encode(block));
      }
      controller.close();
    },
  });
  return Promise.resolve(
    new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
}

function eventBlock(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
