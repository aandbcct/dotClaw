import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PersonalTasks } from "./PersonalTasks";
import { usePersonalTasks } from "./usePersonalTasks";
import { addDaysToDateInput, dateTimeInputValue, dateTimeToRfc3339 } from "./reminderTime";

afterEach(() => { vi.unstubAllGlobals(); });
const current = { id: "t1", title: "原题", description: "", status: "pending", deadline: null, created_at: "2026-11-18T00:00:00Z", updated_at: "2026-11-18T00:00:00Z", completed_at: null, version: 1, is_overdue: false };
const payload = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const list = (items: unknown[]) => ({ items, timezone: "Asia/Shanghai", as_of: "2026-11-18T00:00:00Z" });
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
      if (url.includes("/personal-tasks?")) return payload(list([created]));
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

  it("读取失败可重试，显式刷新更新摘要且每轮只读一次", async () => {
    let summaryReads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/t1/reminders")) {
        summaryReads += 1;
        if (summaryReads === 1) return payload({ error: { message: "网络不可用" } }, 503);
        if (summaryReads === 2) return payload(reminderPage([
          { ...current, id: "r1", rule: { kind: "at", at: "2026-12-01T09:00:00Z" }, task_id: "t1", timezone: "Asia/Shanghai", status: "active", reason: null, next_fire_at: "2026-12-01T09:00:00Z", version: 1, created_at: current.created_at, updated_at: current.updated_at },
          { ...current, id: "r2", rule: { kind: "at", at: "2026-12-02T09:00:00Z" }, task_id: "t1", timezone: "Asia/Shanghai", status: "active", reason: null, next_fire_at: "2026-12-02T09:00:00Z", version: 1, created_at: current.created_at, updated_at: current.updated_at },
        ]));
        return payload(reminderPage([{ ...current, id: "r3", rule: { kind: "at", at: "2026-12-03T09:00:00Z" }, task_id: "t1", timezone: "Asia/Shanghai", status: "active", reason: null, next_fire_at: "2026-12-03T09:00:00Z", version: 1, created_at: current.created_at, updated_at: current.updated_at }]));
      }
      return payload(list([current]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await screen.findByText(/提醒摘要读取失败/);
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText(/另有 1 条/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "刷新" }));
    await screen.findByText(/12月3日/);
    expect(summaryReads).toBe(3);
  });

  it("摘要旧请求晚到时不会覆盖较新的读取", async () => {
    let finishOld: ((response: Response) => void) | undefined;
    let reads = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/t1/reminders")) {
        reads += 1;
        if (reads === 1) return new Promise<Response>((resolve) => { finishOld = resolve; });
        return Promise.resolve(payload(reminderPage([])));
      }
      return Promise.resolve(payload(list([])));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result, unmount } = renderHook(() => usePersonalTasks());
    await waitFor(() => expect(result.current.loading).toBe(false));
    let oldRequest: Promise<void> | undefined;
    await act(async () => {
      oldRequest = result.current.refreshReminderSummary("t1");
      await result.current.refreshReminderSummary("t1");
    });
    finishOld?.(payload(reminderPage([{
      ...current, id: "old", task_id: "t1", rule: { kind: "at", at: "2030-01-01T12:01:00Z" }, timezone: "UTC", status: "active", reason: null, next_fire_at: "2030-01-01T12:01:00Z", version: 1,
    }])));
    await act(async () => { await oldRequest; });
    expect(result.current.reminderSummaries.t1.items).toEqual([]);
    unmount();
  });

  it("切换筛选不会留下永久加载的提醒摘要", async () => {
    let finishSummary: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/t1/reminders")) return new Promise<Response>((resolve, reject) => {
        finishSummary = resolve;
        init?.signal?.addEventListener("abort", () => reject(new DOMException("已取消", "AbortError")), { once: true });
      });
      return Promise.resolve(payload(list([current])));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await screen.findByText("正在读取提醒摘要…");
    await user.click(screen.getByRole("button", { name: "全部" }));
    finishSummary?.(payload(reminderPage([])));
    expect(await screen.findByText("无待触发提醒")).toBeTruthy();
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
