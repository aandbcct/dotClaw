import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReminderEditor } from "./ReminderEditor";
import type { PersonalTask } from "./types";

afterEach(() => vi.useRealTimers());

const makeTask = (deadline: PersonalTask["deadline"]): PersonalTask => ({ id: "task-1", title: "准备评审", description: "", status: "pending", deadline, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", completed_at: null, version: 1, is_overdue: false });
const common = { reminder: null, timezone: "Asia/Shanghai", pending: false, onReload: undefined, onClose: () => undefined };

describe("提醒时间快捷项", () => {
  it("当地今天15点已过时禁用该快捷项", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-20T08:01:00Z"));
    render(<ReminderEditor {...common} task={makeTask(null)} onSave={async () => undefined} />);
    expect((screen.getByRole("button", { name: "今天 15:00" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("明天快捷项按应用时区跨年，并在切换规则后显示有效日期字段", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-12-31T15:00:00Z"));
    const onSave = vi.fn(async () => undefined);
    render(<ReminderEditor {...common} task={makeTask(null)} onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: "明天 09:00" }));
    expect((screen.getByLabelText("触发日期") as HTMLInputElement).value).toBe("2027-01-01");
    expect((screen.getByLabelText("触发时刻") as HTMLInputElement).value).toBe("09:00");
    fireEvent.change(screen.getByLabelText("提醒规则"), { target: { value: "daily" } });
    expect(screen.queryByLabelText("触发日期")).toBeNull();
    fireEvent.change(screen.getByLabelText("提醒规则"), { target: { value: "at" } });
    expect((screen.getByLabelText("触发日期") as HTMLInputElement).value).toBe("2027-01-01");
  });

  it("日期截止前1天跨月默认09点，之后仍可编辑并按应用时区保存", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async () => undefined);
    render(<ReminderEditor {...common} task={makeTask({ kind: "date", value: "2026-03-01" })} onSave={onSave} />);
    await user.click(screen.getByRole("button", { name: "截止前1天（09:00）" }));
    expect((screen.getByLabelText("触发日期") as HTMLInputElement).value).toBe("2026-02-28");
    expect((screen.getByLabelText("触发时刻") as HTMLInputElement).value).toBe("09:00");
    fireEvent.change(screen.getByLabelText("触发时刻"), { target: { value: "10:30" } });
    await user.click(screen.getByRole("button", { name: "保存提醒" }));
    expect(onSave).toHaveBeenCalledWith({ kind: "at", at: "2026-02-28T02:30:00.000Z" }, undefined);
  });

  it("datetime截止前1天沿用当地时刻并跨月换算", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async () => undefined);
    render(<ReminderEditor {...common} task={makeTask({ kind: "datetime", value: "2026-04-01T00:30:00Z" })} onSave={onSave} />);
    await user.click(screen.getByRole("button", { name: "截止前1天（同当地时刻）" }));
    expect((screen.getByLabelText("触发日期") as HTMLInputElement).value).toBe("2026-03-31");
    expect((screen.getByLabelText("触发时刻") as HTMLInputElement).value).toBe("08:30");
    await user.click(screen.getByRole("button", { name: "保存提醒" }));
    expect(onSave).toHaveBeenCalledWith({ kind: "at", at: "2026-03-31T00:30:00.000Z" }, undefined);
  });
});
