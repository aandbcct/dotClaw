import { describe, expect, it } from "vitest";
import { addDaysToDateInput, dateTimeInputValue, dateTimeToRfc3339 } from "./reminderTime";

describe("提醒时区输入", () => {
  it("按应用时区转换为 RFC3339 UTC，并读取既有带偏移值", () => {
    expect(dateTimeToRfc3339("2026-12-02", "10:30", "Asia/Shanghai")).toBe("2026-12-02T02:30:00.000Z");
    expect(dateTimeInputValue("2026-12-02T02:30:00Z", "Asia/Shanghai")).toEqual({ date: "2026-12-02", time: "10:30" });
  });

  it("夏令时重复时刻取较早实例，跳过不存在的时刻", () => {
    expect(dateTimeToRfc3339("2026-11-01", "01:30", "America/New_York")).toBe("2026-11-01T05:30:00.000Z");
    expect(() => dateTimeToRfc3339("2026-03-08", "02:30", "America/New_York")).toThrow(/不存在/);
  });

  it("按日历日跨月递增，不受浏览器本地时区影响", () => {
    expect(addDaysToDateInput("2026-12-31", 1)).toBe("2027-01-01");
  });
});
