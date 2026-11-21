import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { formatNotificationBody, NotificationCenter } from "./NotificationCenter";

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
const page = (items: unknown[], next_before_id: number | null = null) => ({ items, next_before_id, unread_count: 1, watermark: 12 });
const props = { onOpenNavigation: () => undefined, onOpenTask: (_id: string) => undefined, onOpenSummary: (_id: number) => undefined, onRefresh: async () => undefined, revision: 0, unreadCount: 1, connectionError: "", permission: "default" as const, permissionPending: false, permissionError: "", onEnableSystemNotifications: () => undefined };

describe("通知中心", () => {
  it("把普通提醒的时间格式化为浏览器当地时间，保留纯日期截止日和未知正文", () => {
    const readable = formatNotificationBody("reminder", "提醒时间：2026-11-20T01:01:00+00:00；事务截止：2026-11-20");
    expect(readable).toContain("浏览器当地时间");
    expect(readable).toContain(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(readable).toContain("事务截止：2026-11-20");
    expect(readable).not.toContain("2026-11-20T01:01:00+00:00");
    const summary = "离线期间有 1 件事务错过提醒";
    expect(formatNotificationBody("recovery_summary", summary)).toBe(summary);
    expect(formatNotificationBody("reminder", "自定义提醒正文")).toBe("自定义提醒正文");
  });

  it("支持无截止和RFC3339截止时间的固定提醒正文", () => {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const noDeadline = formatNotificationBody("reminder", "提醒时间：2026-11-20T01:01:00+00:00");
    expect(noDeadline).toContain("浏览器当地时间");
    expect(noDeadline).toContain(timezone);
    expect(noDeadline).not.toContain("事务截止");
    const datetimeDeadline = formatNotificationBody("reminder", "提醒时间：2026-11-20T01:01:00+00:00；事务截止：2026-11-20T08:30:00+08:00");
    expect(datetimeDeadline).toContain("事务截止：");
    expect(datetimeDeadline).toContain(timezone);
    expect(datetimeDeadline).not.toContain("2026-11-20T08:30:00+08:00");
  });

  it("在列表中也显示格式化后的提醒时间", async () => {
    const reminder = { ...notification, kind: "reminder" as const, body: "提醒时间：2026-11-20T01:01:00+00:00；事务截止：2026-11-20" };
    vi.stubGlobal("fetch", vi.fn(async () => json(page([reminder]))));
    render(<NotificationCenter {...props} />);
    expect(await screen.findByText(formatNotificationBody("reminder", reminder.body))).toBeTruthy();
    expect(screen.queryByText(reminder.body)).toBeNull();
  });

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
    render(<NotificationCenter {...props} onOpenTask={onOpenTask} onOpenSummary={onOpenSummary} onRefresh={onRefresh} />);

    await user.click(await screen.findByRole("button", { name: /离线期间提醒汇总/ }));
    expect(document.querySelector(".notification-workspace__detail")?.textContent).toContain("当前状态：进行中");
    expect(screen.getByRole("button", { name: "查看摘要事务" })).toBeTruthy();
    const memberDetails = document.querySelector(".notification-workspace__more-members") as HTMLDetailsElement;
    const deliveryDetails = document.querySelector(".notification-workspace__deliveries") as HTMLDetailsElement;
    expect(memberDetails.open).toBe(false);
    expect(deliveryDetails.open).toBe(false);
    expect(onRefresh).toHaveBeenCalledOnce();
    await user.click(screen.getByText("查看相关事务（2）"));
    await user.click(screen.getByText("投递状态（1）"));
    expect(screen.getByText("结果未确认")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "准备评审" }));
    await user.click(screen.getByRole("button", { name: "查看摘要事务" }));
    expect(onOpenTask).toHaveBeenCalledWith("task-1");
    expect(onOpenSummary).toHaveBeenCalledWith(12);
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/notifications/12/read" && init?.method === "POST")).toBe(true);
  });

  it("单成员提醒突出查看事务主操作并反馈已读", async () => {
    const reminder = { ...notification, kind: "reminder" as const, body: "提醒时间：2026-11-20T01:00:00Z", members: [notification.members[0]] };
    let readAt: string | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/notifications?")) return json(page([{ ...reminder, read_at: readAt }]));
      if (url === "/api/v1/notifications/12") return json({ ...reminder, read_at: readAt });
      if (url === "/api/v1/notifications/12/read") { readAt = "2026-11-20T01:05:00Z"; return json({ ...reminder, read_at: readAt }); }
      throw new Error(`未预期的请求：${url}`);
    }));
    const user = userEvent.setup();
    const onOpenTask = vi.fn();
    render(<NotificationCenter {...props} onOpenTask={onOpenTask} />);
    await user.click(await screen.findByRole("button", { name: /离线期间提醒汇总/ }));
    await user.click(screen.getByRole("button", { name: "查看事务：准备评审" }));
    expect(onOpenTask).toHaveBeenCalledWith("task-1");
    expect(await screen.findByRole("status")).toBeTruthy();
    expect(screen.getByRole("button", { name: "查看事务：准备评审" })).toBeTruthy();
    expect(document.querySelector(".notification-workspace__more-members")).toBeNull();
  });

  it("读取列表失败时保留通知区并显示错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: { code: "personal_task_storage_failed", message: "通知暂时无法读取", retryable: true } }, 500)));
    render(<NotificationCenter {...props} />);
    expect((await screen.findByRole("alert")).textContent).toContain("通知暂时无法读取");
  });

  it("自动刷新首屏时保留已加载的历史页", async () => {
    let newest = [notification];
    const older = { ...notification, id: 11, kind: "reminder", title: "较早通知", read_at: "2026-11-19T01:00:00Z" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("before_id=11")) return json(page([older], null));
      return json(page(newest, 11));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const { rerender } = render(<NotificationCenter {...props} />);
    await screen.findByRole("button", { name: /离线期间提醒汇总/ });
    await user.click(screen.getByRole("button", { name: "加载更早通知" }));
    expect(await screen.findByRole("button", { name: /较早通知/ })).toBeTruthy();
    newest = [{ ...notification, id: 13, title: "刚到的新通知" }, notification];
    rerender(<NotificationCenter {...props} revision={1} unreadCount={2} />);
    expect(await screen.findByRole("button", { name: /刚到的新通知/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /较早通知/ })).toBeTruthy();
    expect(screen.getByText("2 条未读通知")).toBeTruthy();
  });

  it("快速切换详情时忽略较早请求的迟到响应", async () => {
    let resolveFirst!: (response: Response) => void;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/notifications?") ) return Promise.resolve(json(page([notification, { ...notification, id: 13, title: "第二条通知" }])));
      if (url === "/api/v1/notifications/12") return new Promise<Response>((resolve) => { resolveFirst = resolve; });
      if (url === "/api/v1/notifications/13") return Promise.resolve(json({ ...notification, id: 13, title: "第二条通知", read_at: "2026-11-20T01:05:00Z" }));
      if (url.endsWith("/read")) return Promise.resolve(json(notification));
      throw new Error(`未预期的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<NotificationCenter {...props} />);
    await user.click(await screen.findByRole("button", { name: /离线期间提醒汇总/ }));
    await user.click(screen.getByRole("button", { name: /第二条通知/ }));
    expect(await screen.findByRole("heading", { name: "第二条通知" })).toBeTruthy();
    resolveFirst(json(notification));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByRole("heading", { name: "第二条通知" })).toBeTruthy();
  });

  it("权限状态使用易懂中文，且请求期间禁用按钮", async () => {
    let resolvePermission!: () => void;
    const onEnableSystemNotifications = vi.fn(() => new Promise<void>((resolve) => { resolvePermission = resolve; }));
    const user = userEvent.setup();
    const { rerender } = render(<NotificationCenter {...props} onEnableSystemNotifications={onEnableSystemNotifications} />);
    await user.click(screen.getByRole("button", { name: "启用系统通知" }));
    rerender(<NotificationCenter {...props} permissionPending onEnableSystemNotifications={onEnableSystemNotifications} />);
    expect(screen.getByRole("button", { name: "正在请求权限…" }).hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText("default")).toBeNull();
    resolvePermission();
  });
});
