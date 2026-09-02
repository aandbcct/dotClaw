import { render, screen, waitFor } from "@testing-library/react";
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
});

describe("dotClaw GUI", () => {
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
