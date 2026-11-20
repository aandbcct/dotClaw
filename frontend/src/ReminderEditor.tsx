import { useState } from "react";
import { ApiError } from "./api";
import { dateTimeInputValue, dateTimeToRfc3339 } from "./reminderTime";
import type { PersonalTask, Reminder, ReminderRule } from "./types";

type OffsetUnit = "minutes" | "hours" | "days";
const unitMinutes: Record<OffsetUnit, number> = { minutes: 1, hours: 60, days: 1440 };

export function ReminderEditor({ task, reminder, timezone, pending, onSave, onReload, onClose }: { task: PersonalTask; reminder: Reminder | null; timezone: string; pending: boolean; onSave: (rule: ReminderRule, baseReminder?: Reminder) => Promise<void>; onReload?: () => Promise<Reminder | null>; onClose: () => void }) {
  const initial = reminder?.rule || { kind: "at", at: "" } as ReminderRule;
  const initialAt = initial.kind === "at" && initial.at ? dateTimeInputValue(initial.at, timezone) : { date: "", time: "09:00" };
  const initialOffset = initial.kind === "before_deadline" ? initial.offset_minutes : 0;
  const [kind, setKind] = useState(initial.kind);
  const [atDate, setAtDate] = useState(initialAt.date);
  const [atTime, setAtTime] = useState(initialAt.time);
  const [amount, setAmount] = useState(initial.kind === "before_deadline" ? initialOffset % 1440 === 0 ? initialOffset / 1440 : initialOffset % 60 === 0 ? initialOffset / 60 : initialOffset : initial.kind === "before_date" ? initial.days_before : 0);
  const [unit, setUnit] = useState<OffsetUnit>(initial.kind === "before_deadline" ? initialOffset % 1440 === 0 ? "days" : initialOffset % 60 === 0 ? "hours" : "minutes" : "minutes");
  const [localTime, setLocalTime] = useState("local_time" in initial ? initial.local_time : "09:00");
  const [weekdays, setWeekdays] = useState<number[]>(initial.kind === "weekly" ? initial.weekdays : [0]);
  const [day, setDay] = useState(initial.kind === "monthly" || initial.kind === "yearly" ? initial.day : 1);
  const [month, setMonth] = useState(initial.kind === "yearly" ? initial.month : 1);
  const [error, setError] = useState("");
  const [baseReminder, setBaseReminder] = useState(reminder);
  const [conflictReminder, setConflictReminder] = useState<Reminder | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    let rule: ReminderRule;
    try {
      if (kind === "at") rule = { kind, at: dateTimeToRfc3339(atDate, atTime, timezone) };
      else if (kind === "before_deadline") {
        const offset = amount * unitMinutes[unit];
        if (!Number.isInteger(offset)) throw new Error("提前时长必须换算为整分钟");
        rule = { kind, offset_minutes: offset };
      }
      else if (kind === "before_date") rule = { kind, days_before: Number(amount), local_time: localTime };
      else if (kind === "daily") rule = { kind, local_time: localTime };
      else if (kind === "weekly") rule = { kind, weekdays: [...weekdays].sort((a, b) => a - b), local_time: localTime };
      else if (kind === "monthly") rule = { kind, day: Number(day), local_time: localTime };
      else rule = { kind: "yearly", month: Number(month), day: Number(day), local_time: localTime };
      await onSave(rule, baseReminder || undefined);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
      if (reason instanceof ApiError && reason.code === "reminder_version_conflict") setConflictReminder(null);
    }
  }

  return <form onSubmit={(event) => void submit(event)} className="reminder-editor reminder-form">
    <label>提醒规则<select disabled={pending} value={kind} onChange={(event) => setKind(event.target.value as ReminderRule["kind"])}><option value="at">指定时刻</option><option value="before_deadline" disabled={task.deadline?.kind !== "datetime"}>截止前</option><option value="before_date" disabled={task.deadline?.kind !== "date"}>日期截止前</option><option value="daily">每天</option><option value="weekly">每周</option><option value="monthly">每月</option><option value="yearly">每年</option></select></label>
    {kind === "at" && <label>触发日期和时刻 · {timezone}<span className="reminder-date-fields"><input aria-label="触发日期" disabled={pending} required type="date" value={atDate} onChange={(event) => setAtDate(event.target.value)} /><input aria-label="触发时刻" disabled={pending} required type="time" value={atTime} onChange={(event) => setAtTime(event.target.value)} /></span></label>}
    {kind === "before_deadline" && <label>提前时长<span className="reminder-offset-fields"><input aria-label="提前数量" disabled={pending} required type="number" min="0" step="any" value={amount} onChange={(event) => setAmount(Number(event.target.value))} /><select aria-label="提前单位" disabled={pending} value={unit} onChange={(event) => setUnit(event.target.value as OffsetUnit)}><option value="minutes">分钟</option><option value="hours">小时</option><option value="days">天</option></select></span></label>}
    {kind === "before_date" && <label>提前天数<input disabled={pending} required type="number" min="0" step="1" value={amount} onChange={(event) => setAmount(Number(event.target.value))} /></label>}
    {kind === "weekly" && <fieldset disabled={pending}><legend>重复星期</legend>{[0, 1, 2, 3, 4, 5, 6].map((value) => <label key={value}><input type="checkbox" checked={weekdays.includes(value)} onChange={(event) => setWeekdays(event.target.checked ? [...weekdays, value] : weekdays.filter((item) => item !== value))} />{["一", "二", "三", "四", "五", "六", "日"][value]}</label>)}</fieldset>}
    {(kind === "monthly" || kind === "yearly") && <label>日期<input disabled={pending} required type="number" min="1" max="31" value={day} onChange={(event) => setDay(Number(event.target.value))} /></label>}
    {kind === "yearly" && <label>月份<input disabled={pending} required type="number" min="1" max="12" value={month} onChange={(event) => setMonth(Number(event.target.value))} /></label>}
    {["before_date", "daily", "weekly", "monthly", "yearly"].includes(kind) && <label>当地时间 · {timezone}<input disabled={pending} required type="time" value={localTime} onChange={(event) => setLocalTime(event.target.value)} /></label>}
    {kind === "monthly" && <p className="reminder-note">当月没有所选日期时跳过，不会提前到月末。</p>}
    {kind === "yearly" && month === 2 && day === 29 && <p className="reminder-note">平年没有 2 月 29 日时跳过。</p>}
    {error && <p role="alert" className="reminder-error">{error}</p>}
    {baseReminder && onReload && <div className="reminder-conflict">{error && !conflictReminder && <button type="button" disabled={pending} onClick={() => { void onReload().then((latest) => { if (!latest) throw new Error("提醒已不存在，无法重新读取"); setConflictReminder(latest); setError(""); }).catch((reason) => setError(reason instanceof Error ? reason.message : "重新读取失败")); }}>重新读取当前提醒</button>}{conflictReminder && <><p>当前版本为 {conflictReminder.version}。确认后保留本次草稿，并按此版本再次提交。</p><button type="button" disabled={pending} onClick={() => { setBaseReminder(conflictReminder); setConflictReminder(null); setError(""); }}>确认使用当前版本</button></>}</div>}
    <footer><button type="button" disabled={pending} onClick={onClose}>返回提醒</button><button className="reminder-primary" type="submit" disabled={pending}>保存提醒</button></footer>
  </form>;
}
