import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useNotifications } from "./useNotifications";
import type { NotificationDeliveryPort } from "./notificationDelivery";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

const item = {
  id: 2,
  kind: "reminder",
  occurrence_id: "occ-2",
  created_at: "2026-11-20T01:00:00Z",
  read_at: null,
  title: "准备评审",
  body: "提醒时间：2026-11-20T01:00:00Z",
  members: [{ task_id: "task-1", title_snapshot: "准备评审", missed: false, overdue: false, current_status: "pending", current_is_overdue: false }],
  deliveries: [],
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function Harness({ delivery }: { delivery: NotificationDeliveryPort }) {
  const state = useNotifications(delivery, () => undefined);
  return <div><span>{state.unreadCount}</span><p role="alert">{state.connectionError}</p></div>;
}

async function settlePromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("全局通知轮询", () => {
  it("投递失败时回报 failed 和错误文本", async () => {
    vi.useFakeTimers();
    const reports: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/v1/notifications?")) return json({ items: [], next_before_id: null, unread_count: 1, watermark: 1 });
      if (url.startsWith("/api/v1/notifications/changes?")) return json({ items: [item], next_after_id: 2, has_more: false, unread_count: 1, watermark: 2 });
      if (url === "/api/v1/notifications/2/deliveries/claim") return json({ notification_id: 2, channel: "browser", claim_id: "claim-2", status: "attempted", attempted_at: "2026-11-20T01:00:01Z", reported_at: null, error: null });
      if (url === "/api/v1/notifications/2/deliveries/report") {
        reports.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return json({ notification_id: 2, channel: "browser", claim_id: "claim-2", ...(JSON.parse(String(init?.body)) as object), attempted_at: "2026-11-20T01:00:01Z", reported_at: "2026-11-20T01:00:02Z" });
      }
      throw new Error(`未预期的请求：${init?.method || "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const delivery: NotificationDeliveryPort = { permission: () => "granted", requestPermission: async () => "granted", deliver: async () => { throw new Error("系统通知被拒绝"); } };
    render(<Harness delivery={delivery} />);
    await settlePromises();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });

    expect(reports).toEqual([{ claim_id: "claim-2", status: "failed", error: "系统通知被拒绝" }]);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/deliveries/report"))).toBe(true);
  });

  it("accepted 回报网络失败时不伪报浏览器投递失败", async () => {
    vi.useFakeTimers();
    const reports: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/v1/notifications?")) return json({ items: [], next_before_id: null, unread_count: 1, watermark: 1 });
      if (url.startsWith("/api/v1/notifications/changes?")) return json({ items: [item], next_after_id: 2, has_more: false, unread_count: 1, watermark: 2 });
      if (url === "/api/v1/notifications/2/deliveries/claim") return json({ notification_id: 2, channel: "browser", claim_id: "claim-2", status: "attempted", attempted_at: "2026-11-20T01:00:01Z", reported_at: null, error: null });
      if (url === "/api/v1/notifications/2/deliveries/report") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        reports.push(body);
        return body.status === "accepted" ? json({ error: { code: "notification_not_found", message: "回报暂时失败", retryable: true } }, 500) : json(body);
      }
      throw new Error(`未预期的请求：${init?.method || "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const delivery: NotificationDeliveryPort = { permission: () => "granted", requestPermission: async () => "granted", deliver: async () => undefined };
    render(<Harness delivery={delivery} />);
    await settlePromises();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });

    expect(reports).toEqual([{ claim_id: "claim-2", status: "accepted", error: null }]);
    expect(screen.getByRole("alert").textContent).toContain("回报暂时失败");
  });
});
