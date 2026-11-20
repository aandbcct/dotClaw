import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotificationCenter } from "./NotificationCenter";

afterEach(() => { vi.unstubAllGlobals(); });

const notification = {
  id: 12,
  kind: "recovery_summary",
  occurrence_id: null,
  created_at: "2026-11-20T01:00:00Z",
  read_at: null,
  title: "离线期间提醒汇总",
  body: "离线期间有 1 件事务错过提醒，目前有 2 件事务已逾期",
  members: [
    { task_id: "task-1", title_snapshot: "准备评审", missed: true, overdue: true, current_status: "in_progress", current_is_overdue: true },
    { task_id: "task-2", title_snapshot: "整理材料", missed: false, overdue: true, current_status: "pending", current_is_overdue: true },
  ],
  deliveries: [{ notification_id: 12, channel: "browser", claim_id: "claim-12", status: "attempted", attempted_at: "2026-11-20T01:00:01Z", reported_at: null, error: null }],
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const page = (items: unknown[]) => ({ items, next_before_id: null, unread_count: 1, watermark: 12 });

describe("通知中心", () => {
  it("展示通知详情和成员状态，并分别导航到事务与摘要", async () => {
    let readAt: string | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/v1/notifications?") && init?.method !== "POST") return json(page([{ ...notification, read_at: readAt }]));
      if (url === "/api/v1/notifications/12" && init?.method !== "POST") return json({ ...notification, read_at: readAt });
      if (url === "/api/v1/notifications/12/read" && init?.method === "POST") {
        readAt = "2026-11-20T01:05:00Z";
        return json({ ...notification, read_at: readAt });
      }
      throw new Error(`未预期的请求：${init?.method || "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onOpenTask = vi.fn();
    const onOpenSummary = vi.fn();
    const onRefresh = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<NotificationCenter onOpenNavigation={() => undefined} systemNotifications={null} onOpenTask={onOpenTask} onOpenSummary={onOpenSummary} onRefresh={onRefresh} />);

    await user.click(await screen.findByRole("button", { name: /离线期间提醒汇总/ }));
    expect(document.querySelector(".notification-detail")?.textContent).toContain("当前状态：in_progress");
    expect(screen.getByText("结果未确认")).toBeTruthy();
    expect(onRefresh).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "准备评审" }));
    await user.click(screen.getByRole("button", { name: "查看摘要事务" }));
    expect(onOpenTask).toHaveBeenCalledWith("task-1");
    expect(onOpenSummary).toHaveBeenCalledWith(12);
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/notifications/12/read" && init?.method === "POST")).toBe(true);
  });

  it("读取列表失败时保留通知区并显示错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: { code: "personal_task_storage_failed", message: "通知暂时无法读取", retryable: true } }, 500)));
    render(<NotificationCenter onOpenNavigation={() => undefined} systemNotifications={null} onOpenTask={() => undefined} onOpenSummary={() => undefined} onRefresh={async () => undefined} />);
    expect((await screen.findByRole("alert")).textContent).toContain("通知暂时无法读取");
  });
});
