import { describe, expect, it } from "vitest";
import { reminderRuleSummary } from "./reminderPresentation";

describe("提醒规则摘要", () => {
  it("用中文表达周期、提前时长和不存在日期的处理", () => {
    expect(reminderRuleSummary({ kind: "weekly", weekdays: [0, 2], local_time: "09:00" }, "Asia/Shanghai")).toBe("每周一、周三 09:00");
    expect(reminderRuleSummary({ kind: "before_deadline", offset_minutes: 1440 }, "Asia/Shanghai")).toBe("截止前1天");
    expect(reminderRuleSummary({ kind: "monthly", day: 31, local_time: "09:00" }, "Asia/Shanghai")).toContain("当月无此日期时跳过");
    expect(reminderRuleSummary({ kind: "yearly", month: 2, day: 29, local_time: "09:00" }, "Asia/Shanghai")).toContain("平年跳过");
    expect(reminderRuleSummary({ kind: "at", at: "2026-12-02T02:30:00Z" }, "Asia/Shanghai")).toContain("10:30");
  });
});
