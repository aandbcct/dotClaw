import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PersonalTasks } from "./PersonalTasks";
import { addDaysToDateInput, dateTimeInputValue, dateTimeToRfc3339 } from "./reminderTime";

afterEach(() => { vi.unstubAllGlobals(); });
const current = { id: "t1", title: "原题", description: "", status: "pending", deadline: null, created_at: "2026-11-18T00:00:00Z", updated_at: "2026-11-18T00:00:00Z", completed_at: null, version: 1, is_overdue: false };
const payload = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const list = (items: unknown[], extra: Record<string, unknown> = {}) => ({ items: items.map((item) => ({ ...(item as Record<string, unknown>), reminder_summary: (item as { reminder_summary?: unknown }).reminder_summary || { active_count: 0, next_fire_at: null } })), timezone: "Asia/Shanghai", as_of: "2026-11-18T00:00:00Z", total: items.length, offset: 0, limit: 50, has_more: false, ...extra });
const reminderPage = (items: unknown[]) => ({ items, timezone: "Asia/Shanghai", scheduler_enabled: true, last_scan_at: null, scan_lag_seconds: 0 });

describe("个人事务视图", () => {
  it("新建时将事务和初始提醒放进同一个创建请求", async () => {
    const created = { ...current, id: "new-task", title: "含提醒事务" };
    const today = dateTimeInputValue(new Date().toISOString(), "Asia/Shanghai").date;
    const trigger = dateTimeToRfc3339(addDaysToDateInput(today, 1), "09:00", "Asia/Shanghai");
    const initialReminder = { id: "r1", task_id: "new-task", rule: { kind: "at", at: trigger }, timezone: "Asia/Shanghai", status: "active", reason: null, next_fire_at: trigger, version: 1, created_at: current.created_at, updated_at: current.updated_at };
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); requests.push({ url, init });
      if (url === "/api/v1/personal-tasks" && init?.method === "POST") return payload(created, 201);
      if (url.endsWith("/new-task/reminders")) return payload(reminderPage([initialReminder]));
      if (url.endsWith("/new-task")) return payload(created);
      if (url.includes("/personal-tasks?")) return payload(list([{ ...created, reminder_summary: { active_count: 1, next_fire_at: trigger } }]));
      throw new Error("未预期请求：" + (init?.method || "GET") + " " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "新建" }));
    await user.type(screen.getByLabelText("标题"), "含提醒事务");
    await user.click(screen.getByRole("button", { name: "明天 09:00" }));
    await user.click(screen.getByRole("button", { name: "保存事务" }));

    await screen.findByText("事务及提醒已创建");
    expect(screen.queryByRole("dialog")).toBeNull();
    await screen.findByText(/下次提醒：/);
    const create = requests.find(({ init }) => init?.method === "POST");
    expect(JSON.parse(String(create?.init?.body))).toMatchObject({
      title: "含提醒事务",
      reminders: [{ kind: "at", at: dateTimeToRfc3339(addDaysToDateInput(today, 1), "09:00", "Asia/Shanghai") }],
    });
    expect(requests.filter(({ init }) => init?.method === "POST")).toHaveLength(1);
  });

  it("日期截止的快捷提醒保留明确的当地触发时刻", async () => {
    const created = { ...current, id: "date-task", title: "日期截止事务", deadline: { kind: "date", value: "2032-05-10" } };
    let body = "";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/personal-tasks" && init?.method === "POST") { body = String(init.body); return payload(created, 201); }
      if (url.endsWith("/date-task/reminders")) return payload(reminderPage([]));
      if (url.endsWith("/date-task")) return payload(created);
      if (url.includes("/personal-tasks?")) return payload(list([created]));
      throw new Error("未预期请求：" + (init?.method || "GET") + " " + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "新建" }));
    await user.type(screen.getByLabelText("标题"), "日期截止事务");
    await user.selectOptions(screen.getByLabelText("截止类型"), "date");
    fireEvent.change(screen.getByLabelText("截止日期"), { target: { value: "2032-05-10" } });
    await user.click(screen.getByRole("button", { name: "截止前 1 天" }));
    await user.click(screen.getByRole("button", { name: "保存事务" }));

    await waitFor(() => expect(body).not.toBe(""));
    expect(JSON.parse(body)).toMatchObject({
      deadline: { kind: "date", value: "2032-05-10" },
      reminders: [{ kind: "before_date", days_before: 1, local_time: "09:00" }],
    });
  });

  it("关键词和日期筛选随查询发送，摘要直接来自列表项且聚焦刷新当前范围", async () => {
    let listReads = 0;
    const withSummary = { ...current, reminder_summary: { active_count: 2, next_fire_at: "2026-12-01T09:00:00Z" } };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/personal-tasks?")) { listReads += 1; return payload(list([withSummary])); }
      throw new Error("未预期请求：" + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    expect(await screen.findByText(/下次提醒：/)).toBeTruthy();
    await user.type(screen.getByLabelText("事务关键词"), "%_");
    await user.clear(screen.getByLabelText("截止日期从")); await user.type(screen.getByLabelText("截止日期从"), "2026-11-20");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(listReads).toBe(3));
    const searchUrl = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), "http://localhost");
    expect(searchUrl.searchParams.get("keyword")).toBe("%_");
    expect(searchUrl.searchParams.get("deadline_from")).toBe("2026-11-20");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/reminders"))).toBe(false);
    await user.click(screen.getByLabelText("仅逾期"));
    await waitFor(() => {
      const query = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), "http://localhost").searchParams;
      expect(query.get("overdue_only")).toBe("true"); expect(query.get("keyword")).toBe("%_");
    });
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await waitFor(() => expect(listReads).toBe(5));
  });

  it("加载更多追加无重复事务，搜索后重置分页并传入全部筛选", async () => {
    const second = { ...current, id: "t2", title: "第二页" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.searchParams.get("keyword") === "第二页") return payload(list([second]));
      if (url.searchParams.get("offset") === "50") return payload(list([current, second], { offset: 50, total: 51, has_more: false }));
      return payload(list([current, ...Array.from({ length: 49 }, (_, index) => ({ ...current, id: `more-${index}`, title: `更多 ${index}` }))], { total: 51, has_more: true }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await screen.findByText("原题");
    await user.click(screen.getByRole("button", { name: "加载更多" }));
    await screen.findByText("第二页");
    expect(screen.getAllByText("原题")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(screen.getAllByText("第二页")).toHaveLength(1));
    expect(screen.getAllByText("原题")).toHaveLength(1);
    await user.type(screen.getByLabelText("事务关键词"), "第二页");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    expect(await screen.findByText("第二页")).toBeTruthy();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => new URL(String(input), "http://localhost").searchParams.get("offset") === "0" && new URL(String(input), "http://localhost").searchParams.get("keyword") === "第二页")).toBe(true));
  });

  it("旧列表响应晚到时不会覆盖新的筛选结果", async () => {
    let finishOld: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.searchParams.get("filter") === "active" && !finishOld) return new Promise<Response>((resolve) => { finishOld = resolve; });
      return Promise.resolve(payload(list([{ ...current, title: "最新结果" }])));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "全部" }));
    expect(await screen.findByText("最新结果")).toBeTruthy();
    finishOld?.(payload(list([{ ...current, title: "旧结果" }])));
    await waitFor(() => expect(screen.queryByText("旧结果")).toBeNull());
  });

  it("列表失败可重试，任务操作展示服务返回的提醒变化数量", async () => {
    let reads = 0;
    const changes = [
      { reminder_id: "r1", action: "rescheduled", reason: null, next_fire_at: "2026-12-01T09:00:00Z", version: 2 },
      { reminder_id: "r2", action: "paused", reason: "deadline_moved_past", next_fire_at: null, version: 2 },
      { reminder_id: "r3", action: "cancelled", reason: "task_ended", next_fire_at: null, version: 2 },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") return payload({ ...current, status: "completed", reminder_changes: changes });
      if (url.includes("/personal-tasks?")) {
        reads += 1;
        if (reads === 1) return payload({ error: { message: "网络不可用" } }, 503);
        return payload(list([current]));
      }
      throw new Error("未预期请求：" + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("原题");
    await user.click(screen.getByRole("button", { name: "完成" }));
    expect(await screen.findByText("事务已完成 · 改期 1 条、暂停 1 条、取消 1 条")).toBeTruthy();
  });

  it("摘要目标超过首屏时定向读取事务，并明确标记真实 404", async () => {
    const target = { ...current, id: "task-51", title: "首屏外事务" };
    const firstPage = [current, ...Array.from({ length: 49 }, (_, index) => ({ ...current, id: `task-${index + 1}`, title: `事务 ${index + 1}` }))];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/notifications/7")) return payload({ id: 7, members: [{ task_id: target.id }, { task_id: "deleted-task" }] });
      if (url.endsWith(`/personal-tasks/${target.id}`)) return payload(target);
      if (url.endsWith("/personal-tasks/deleted-task")) return payload({ error: { code: "personal_task_not_found", message: "事务不存在" } }, 404);
      if (url.includes("/personal-tasks?")) return payload(list(firstPage, { total: 51, has_more: true }));
      throw new Error("未预期请求：" + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PersonalTasks onOpenNavigation={() => undefined} focusNotificationId={7} />);
    expect(await screen.findByText("首屏外事务")).toBeTruthy();
    expect(await screen.findByText("关联事务已删除（deleted-task）")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => new URL(String(url), "http://localhost").searchParams.get("offset") === "50")).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(`/personal-tasks/${target.id}`))).toBe(true);
  });

  it("关联事务非 404 读取失败可重试，且不会误报删除", async () => {
    let taskReads = 0;
    const target = { ...current, id: "task-51", title: "稍后可读事务" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/notifications/8")) return payload({ id: 8, members: [{ task_id: target.id }] });
      if (url.endsWith(`/personal-tasks/${target.id}`)) {
        taskReads += 1;
        return taskReads === 1 ? payload({ error: { code: "temporary_error", message: "暂时不可用" } }, 503) : payload(target);
      }
      if (url.includes("/personal-tasks?")) return payload(list([current]));
      throw new Error("未预期请求：" + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} focusNotificationId={8} />);
    expect(await screen.findByText("部分关联事务读取失败，请重试。")).toBeTruthy();
    expect(screen.queryByText(/关联事务已删除/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("稍后可读事务")).toBeTruthy();
  });

  it("关联事务首次读取时立即显示加载状态", async () => {
    let finishTask: ((response: Response) => void) | undefined;
    const target = { ...current, id: "task-90", title: "稍后返回" };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/notifications/9")) return Promise.resolve(payload({ id: 9, members: [{ task_id: target.id }] }));
      if (url.endsWith(`/personal-tasks/${target.id}`)) return new Promise<Response>((resolve) => { finishTask = resolve; });
      if (url.includes("/personal-tasks?")) return Promise.resolve(payload(list([current])));
      throw new Error("未预期请求：" + url);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PersonalTasks onOpenNavigation={() => undefined} focusNotificationId={9} />);
    expect(await screen.findByText("正在读取关联事务…")).toBeTruthy();
    finishTask?.(payload(target));
    expect(await screen.findByText("稍后返回")).toBeTruthy();
  });

  it("冲突后先读取，再由用户确认替换草稿与版本", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "PUT") return payload({ error: { code: "personal_task_version_conflict", message: "事务已被修改" } }, 409);
      if (url.endsWith("/t1")) return payload({ ...current, title: "服务器标题", version: 2 });
      return payload(list([current]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(await screen.findByText("更多"));
    await user.click(screen.getByRole("button", { name: "编辑事务" }));
    const title = screen.getByLabelText("标题"); await user.clear(title); await user.type(title, "我的草稿");
    await user.click(screen.getByRole("button", { name: "保存事务" }));
    await user.click(await screen.findByRole("button", { name: "重新读取" }));
    expect(await screen.findByText(/确认后会替换当前草稿/)).toBeTruthy();
    expect((title as HTMLInputElement).value).toBe("我的草稿");
    await user.click(screen.getByRole("button", { name: "确认替换草稿" }));
    expect((title as HTMLInputElement).value).toBe("服务器标题");
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/t1"))).toBe(true);
  });

  it("提交期间禁用编辑并只发送一次创建请求", async () => {
    let finishPost: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishPost = resolve; });
      return Promise.resolve(payload(list([])));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "新建" }));
    const title = screen.getByLabelText("标题"); await user.type(title, "新事务");
    fireEvent.click(screen.getByRole("button", { name: "保存事务" }));
    fireEvent.click(screen.getByRole("button", { name: "保存事务" }));
    await waitFor(() => expect((title as HTMLInputElement).disabled).toBe(true));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    finishPost?.(payload({ ...current, title: "新事务" }, 201));
    await waitFor(() => expect(screen.queryByLabelText("标题")).toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("事务已创建")).toBeTruthy();
  });
});
