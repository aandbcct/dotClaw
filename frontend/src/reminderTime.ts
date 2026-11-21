const fields = ["year", "month", "day", "hour", "minute", "second"] as const;

function zonedParts(date: Date, timezone: string): number[] {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  return fields.map((field) => Number(parts.find((part) => part.type === field)?.value));
}

function asWallTime(parts: number[]): number {
  return Date.UTC(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5]);
}

export function dateTimeInputValue(value: string, timezone: string): { date: string; time: string } {
  const parts = zonedParts(new Date(value), timezone);
  return { date: `${String(parts[0]).padStart(4, "0")}-${String(parts[1]).padStart(2, "0")}-${String(parts[2]).padStart(2, "0")}`, time: `${String(parts[3]).padStart(2, "0")}:${String(parts[4]).padStart(2, "0")}` };
}

export function addDaysToDateInput(date: string, days: number): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match) throw new Error("请选择有效的日期");
  const shifted = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + days));
  return `${String(shifted.getUTCFullYear()).padStart(4, "0")}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}-${String(shifted.getUTCDate()).padStart(2, "0")}`;
}

export function dateTimeToRfc3339(date: string, time: string, timezone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const clock = /^(\d{2}):(\d{2})$/.exec(time);
  if (!match || !clock) throw new Error("请选择有效的日期和时间");
  const requested = [Number(match[1]), Number(match[2]), Number(match[3]), Number(clock[1]), Number(clock[2]), 0];
  const wall = asWallTime(requested);
  if (zonedParts(new Date(wall), "UTC").some((value, index) => value !== requested[index])) throw new Error("请选择有效的日期和时间");

  const offsets = new Set<number>();
  for (const hours of [-36, -24, -12, 0, 12, 24, 36]) {
    const instant = wall + hours * 60 * 60 * 1000;
    offsets.add(asWallTime(zonedParts(new Date(instant), timezone)) - instant);
  }
  const matches = [...offsets]
    .map((offset) => wall - offset)
    .filter((instant) => zonedParts(new Date(instant), timezone).every((value, index) => value === requested[index]));
  if (!matches.length) throw new Error("该日期和时间在应用时区中不存在，请选择其他时间");
  return new Date(Math.min(...matches)).toISOString();
}
