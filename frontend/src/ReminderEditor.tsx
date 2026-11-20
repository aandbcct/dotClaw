import { useState } from "react";
import { ApiError } from "./api";
import type { PersonalTask, Reminder, ReminderRule } from "./types";

export function ReminderEditor({ task, reminder, timezone, pending, onSave, onReload, onClose }: { task: PersonalTask; reminder: Reminder | null; timezone: string; pending: boolean; onSave: (rule: ReminderRule, baseReminder?: Reminder) => Promise<void>; onReload?: () => Promise<Reminder | null>; onClose: () => void }) {
  const initial = reminder?.rule || { kind: "at", at: "" } as ReminderRule;
  const [kind, setKind] = useState(initial.kind);
  const [at, setAt] = useState(initial.kind === "at" ? initial.at : "");
  const [amount, setAmount] = useState(initial.kind === "before_deadline" ? initial.offset_minutes : initial.kind === "before_date" ? initial.days_before : 0);
  const [localTime, setLocalTime] = useState("local_time" in initial ? initial.local_time : "09:00");
  const [weekdays, setWeekdays] = useState<number[]>(initial.kind === "weekly" ? initial.weekdays : [0]);
  const [day, setDay] = useState(initial.kind === "monthly" || initial.kind === "yearly" ? initial.day : 1);
  const [month, setMonth] = useState(initial.kind === "yearly" ? initial.month : 1);
  const [error, setError] = useState("");
  const [baseReminder, setBaseReminder] = useState(reminder);
  const [conflictReminder, setConflictReminder] = useState<Reminder | null>(null);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError("");
    let rule: ReminderRule;
    if (kind === "at") rule = { kind, at };
    else if (kind === "before_deadline") rule = { kind, offset_minutes: Number(amount) };
    else if (kind === "before_date") rule = { kind, days_before: Number(amount), local_time: localTime };
    else if (kind === "daily") rule = { kind, local_time: localTime };
    else if (kind === "weekly") rule = { kind, weekdays: [...weekdays].sort((a,b) => a-b), local_time: localTime };
    else if (kind === "monthly") rule = { kind, day: Number(day), local_time: localTime };
    else rule = { kind: "yearly", month: Number(month), day: Number(day), local_time: localTime };
    try { await onSave(rule, baseReminder || undefined); onClose(); }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
      if (reason instanceof ApiError && reason.code === "reminder_version_conflict") setConflictReminder(null);
    }
  }
  return <form onSubmit={(event) => void submit(event)} className="reminder-editor">
    <label>规则<select value={kind} onChange={(event) => setKind(event.target.value as ReminderRule["kind"])}><option value="at">指定时刻</option><option value="before_deadline" disabled={task.deadline?.kind !== "datetime"}>截止前</option><option value="before_date" disabled={task.deadline?.kind !== "date"}>日期截止前</option><option value="daily">每天</option><option value="weekly">每周</option><option value="monthly">每月</option><option value="yearly">每年</option></select></label>
    {kind === "at" && <label>RFC3339 时刻（必须写明时区偏移）<input required placeholder="2026-12-01T09:00:00+08:00" value={at} onChange={(event) => setAt(event.target.value)} /></label>}
    {kind === "before_deadline" && <label>提前分钟数<input required type="number" min="0" value={amount} onChange={(event) => setAmount(Number(event.target.value))} /></label>}
    {kind === "before_date" && <label>提前天数<input required type="number" min="0" value={amount} onChange={(event) => setAmount(Number(event.target.value))} /></label>}
    {kind === "weekly" && <fieldset><legend>星期（周一为 0）</legend>{[0,1,2,3,4,5,6].map((value) => <label key={value}><input type="checkbox" checked={weekdays.includes(value)} onChange={(event) => setWeekdays(event.target.checked ? [...weekdays, value] : weekdays.filter((item) => item !== value))} />{["一","二","三","四","五","六","日"][value]}</label>)}</fieldset>}
    {(kind === "monthly" || kind === "yearly") && <label>日期<input required type="number" min="1" max="31" value={day} onChange={(event) => setDay(Number(event.target.value))} /></label>}
    {kind === "yearly" && <label>月份<input required type="number" min="1" max="12" value={month} onChange={(event) => setMonth(Number(event.target.value))} /></label>}
    {["before_date","daily","weekly","monthly","yearly"].includes(kind) && <label>当地时间（{timezone}）<input required type="time" value={localTime} onChange={(event) => setLocalTime(event.target.value)} /></label>}
    {kind === "monthly" && <p className="muted">该月没有所选日期时跳过，不会提前到月末。</p>}
    {error && <p role="alert">{error}</p>}
    {baseReminder && onReload && <div>{error && !conflictReminder && <button type="button" disabled={pending} onClick={() => { void onReload().then((latest) => { if (!latest) throw new Error("提醒已不存在，无法重新读取"); setConflictReminder(latest); setError(""); }).catch((reason) => setError(reason instanceof Error ? reason.message : "重新读取失败")); }}>重新读取当前提醒</button>}{conflictReminder && <><p>当前版本为 {conflictReminder.version}。确认后保留本次草稿，并按此版本再次提交。</p><button type="button" disabled={pending} onClick={() => { setBaseReminder(conflictReminder); setConflictReminder(null); setError(""); }}>确认替换当前版本</button></>}</div>}
    <footer><button type="button" disabled={pending} onClick={onClose}>取消</button><button type="submit" disabled={pending}>保存</button></footer>
  </form>;
}
