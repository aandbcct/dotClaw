import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskReminders } from "./TaskReminders";
import type { PersonalTask } from "./types";

afterEach(() => { vi.unstubAllGlobals(); });

const task: PersonalTask = { id: "task-1", title: "准备评审", description: "", status: "pending", deadline: null, created_at: "2026-11-20T01:00:00Z", updated_at: "2026-11-20T01:00:00Z", completed_at: null, version: 1, is_overdue: false };
const reminder = { id: "reminder-1", task_id: "task-1", rule: { kind: "at", at: "2026-12-01T09:00:00Z" }, timezone: "Asia/Shanghai", status: "active", reason: null, next_fire_at: "2026-12-01T09:00:00Z", version: 1, created_at: "2026-11-20T01:00:00Z", updated_at: "2026-11-20T01:00:00Z" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const reminderPage = (items: unknown[]) => ({ items, timezone: "Asia/Shanghai", scheduler_enabled: true, last_scan_at: "2026-11-20T01:00:00Z", scan_lag_seconds: 0.1 });

describe("事务提醒管理", () => {
  it("409 后保留规则草稿，确认重新读取的版本后才重提", async () => {
    let latest = reminder;
    let updateCount = 0;
    const putBodies: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/personal-tasks/task-1" && !init?.method) return json({ ...task, reminders: [latest] });
      if (url === "/api/v1/personal-tasks/task-1/reminders" && !init?.method) return json(reminderPage([latest]));
      if (url === "/api/v1/personal-tasks/task-1/reminders/reminder-1" && init?.method === "PUT") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        putBodies.push(body);
        updateCount += 1;
        if (updateCount === 1) {
          latest = { ...reminder, version: 2, updated_at: "2026-11-20T01:02:00Z" };
          return json({ error: { code: "reminder_version_conflict", message: "提醒已被修改，请刷新后重试", retryable: false } }, 409);
        }
        latest = { ...latest, rule: body.rule as typeof reminder.rule, version: 3, updated_at: "2026-11-20T01:03:00Z" };
        return json(latest);
      }
      throw new Error(`未预期的请求：${init?.method || "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<TaskReminders task={task} onClose={() => undefined} onChanged={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: "修改" }));
    const dateInput = screen.getByLabelText("触发日期");
    const timeInput = screen.getByLabelText("触发时刻");
    fireEvent.change(dateInput, { target: { value: "2026-12-02" } });
    fireEvent.change(timeInput, { target: { value: "10:30" } });
    await user.click(screen.getByRole("button", { name: "保存提醒" }));

    await waitFor(() => expect(screen.getAllByRole("alert").some((node) => node.textContent?.includes("提醒已被修改"))).toBe(true));
    expect((dateInput as HTMLInputElement).value).toBe("2026-12-02");
    expect((timeInput as HTMLInputElement).value).toBe("10:30");
    await user.click(screen.getByRole("button", { name: "重新读取当前提醒" }));
    expect(await screen.findByText(/当前版本为 2/)).toBeTruthy();
    expect((dateInput as HTMLInputElement).value).toBe("2026-12-02");
    expect((timeInput as HTMLInputElement).value).toBe("10:30");
    await user.click(screen.getByRole("button", { name: "确认使用当前版本" }));
    await user.click(screen.getByRole("button", { name: "保存提醒" }));

    await waitFor(() => expect(updateCount).toBe(2));
    expect(putBodies.map((body) => body.expected_version)).toEqual([1, 2]);
    expect(putBodies[1]).toMatchObject({ expected_task_version: 1, rule: { kind: "at", at: "2026-12-02T02:30:00.000Z" } });
  });
});
