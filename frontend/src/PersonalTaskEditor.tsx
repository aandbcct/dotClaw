import { useState, type FormEvent } from "react";
import { ApiError } from "./api";
import { addDaysToDateInput, dateTimeInputValue, dateTimeToRfc3339 } from "./reminderTime";
import type { PersonalTask, PersonalTaskInput, ReminderRule, TaskDeadline } from "./types";

type InitialReminderKind = "none" | "at" | "before_deadline" | "before_date";

export function PersonalTaskEditor({ task, timezone, pending, onSave, onReload, onClose }: { task: PersonalTask | null; timezone: string; pending: boolean; onSave: (input: PersonalTaskInput, version?: number, reminder?: ReminderRule) => Promise<void>; onReload: (id: string) => Promise<PersonalTask>; onClose: () => void }) {
  const [title, setTitle] = useState(task?.title || "");
  const [description, setDescription] = useState(task?.description || "");
  const [kind, setKind] = useState<"none" | "date" | "datetime">(task?.deadline?.kind || "none");
  const [date, setDate] = useState(task?.deadline?.kind === "date" ? task.deadline.value : task?.deadline?.kind === "datetime" ? dateTimeInputValue(task.deadline.value, timezone).date : "");
  const [time, setTime] = useState(task?.deadline?.kind === "datetime" ? dateTimeInputValue(task.deadline.value, timezone).time : "09:00");
  const today = dateTimeInputValue(new Date().toISOString(), timezone).date;
  const [initialReminderKind, setInitialReminderKind] = useState<InitialReminderKind>("none");
  const [reminderDate, setReminderDate] = useState(today);
  const [reminderTime, setReminderTime] = useState("15:00");
  const [localTime, setLocalTime] = useState("09:00");
  const [error, setError] = useState("");
  const [version, setVersion] = useState(task?.version);
  const [conflict, setConflict] = useState(false);
  const [candidate, setCandidate] = useState<PersonalTask | null>(null);
  const todayAt15IsPast = Date.parse(dateTimeToRfc3339(today, "15:00", timezone)) <= Date.now();
  const hasDeadline = kind === "date" ? Boolean(date) : kind === "datetime" && Boolean(date && time);

  function selectDeadlineKind(nextKind: "none" | "date" | "datetime") {
    setKind(nextKind);
    if ((initialReminderKind === "before_deadline" && nextKind !== "datetime") || (initialReminderKind === "before_date" && nextKind !== "date")) setInitialReminderKind("none");
  }

  function useDeadlineMinusOneDay() {
    if (kind === "datetime") setInitialReminderKind("before_deadline");
    else if (kind === "date") {
      setInitialReminderKind("before_date");
      setLocalTime("09:00");
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const deadline: TaskDeadline | null = kind === "none" ? null : kind === "date" ? { kind, value: date } : { kind, value: dateTimeToRfc3339(date, time, timezone) };
      let reminder: ReminderRule | undefined;
      if (initialReminderKind === "at") reminder = { kind: "at", at: dateTimeToRfc3339(reminderDate, reminderTime, timezone) };
      else if (initialReminderKind === "before_deadline") reminder = { kind: "before_deadline", offset_minutes: 1440 };
      else if (initialReminderKind === "before_date") reminder = { kind: "before_date", days_before: 1, local_time: localTime };
      await onSave({ title: title.trim(), description, deadline }, version, reminder);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
      setConflict(reason instanceof ApiError && reason.code === "personal_task_version_conflict");
    }
  }

  async function loadCurrent() {
    if (!task) return;
    try { setCandidate(await onReload(task.id)); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "重新读取失败"); }
  }

  function acceptCurrent() {
    if (!candidate) return;
    setTitle(candidate.title);
    setDescription(candidate.description);
    setKind(candidate.deadline?.kind || "none");
    if (candidate.deadline?.kind === "date") setDate(candidate.deadline.value);
    if (candidate.deadline?.kind === "datetime") {
      const value = dateTimeInputValue(candidate.deadline.value, timezone);
      setDate(value.date);
      setTime(value.time);
    }
    setVersion(candidate.version);
    setCandidate(null);
    setConflict(false);
    setError("");
  }

  return <form className="task-editor reminder-form" onSubmit={(event) => void submit(event)}>
    <h2>{task ? "编辑事务" : "新建事务"}</h2>
    <label>标题<input required disabled={pending} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
    <label>说明<textarea disabled={pending} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
    <label>截止类型<select disabled={pending} value={kind} onChange={(event) => selectDeadlineKind(event.target.value as typeof kind)}><option value="none">无截止</option><option value="date">日期</option><option value="datetime">具体时刻</option></select></label>
    {kind !== "none" && <label>{kind === "date" ? "截止日期" : `截止日期和时刻 · ${timezone}`}<span className="reminder-date-fields"><input aria-label="截止日期" disabled={pending} required type="date" value={date} onChange={(event) => setDate(event.target.value)} />{kind === "datetime" && <input aria-label="截止时刻" disabled={pending} required type="time" value={time} onChange={(event) => setTime(event.target.value)} />}</span></label>}
    {task?.deadline && (kind === "none" || kind !== task.deadline.kind) && <p className="reminder-note">保存后，依赖原截止类型的活动提醒会取消。</p>}
    {!task && <fieldset className="initial-reminder">
      <legend>初始提醒</legend>
      <label>添加提醒<select disabled={pending} value={initialReminderKind} onChange={(event) => setInitialReminderKind(event.target.value as InitialReminderKind)}><option value="none">暂不添加</option><option value="at">指定日期和时刻</option><option value="before_deadline" disabled={kind !== "datetime" || !hasDeadline}>具体时刻截止前一天</option><option value="before_date" disabled={kind !== "date" || !hasDeadline}>日期截止前一天</option></select></label>
      {initialReminderKind === "at" && <label>提醒日期和时刻 · {timezone}<span className="reminder-date-fields"><input aria-label="提醒日期" disabled={pending} required type="date" value={reminderDate} onChange={(event) => setReminderDate(event.target.value)} /><input aria-label="提醒时刻" disabled={pending} required type="time" value={reminderTime} onChange={(event) => setReminderTime(event.target.value)} /></span></label>}
      {initialReminderKind === "before_date" && <label>截止前提醒时刻 · {timezone}<input aria-label="截止前提醒时刻" disabled={pending} required type="time" value={localTime} onChange={(event) => setLocalTime(event.target.value)} /></label>}
      {initialReminderKind === "before_deadline" && <p className="reminder-note">将在具体时刻截止前一天提醒。</p>}
      <div className="reminder-quick-actions"><span>快捷选择</span><button type="button" disabled={pending || todayAt15IsPast} onClick={() => { setInitialReminderKind("at"); setReminderDate(today); setReminderTime("15:00"); }}>今天 15:00</button><button type="button" disabled={pending} onClick={() => { setInitialReminderKind("at"); setReminderDate(addDaysToDateInput(today, 1)); setReminderTime("09:00"); }}>明天 09:00</button><button type="button" disabled={pending || !hasDeadline} onClick={useDeadlineMinusOneDay}>截止前 1 天</button></div>
    </fieldset>}
    {error && <p role="alert" className="reminder-error">{error}</p>}
    {conflict && <div className="reminder-conflict"><button type="button" disabled={pending} onClick={() => void loadCurrent()}>重新读取</button>{candidate && <><p>重新读取的版本为 {candidate.version}。确认后会替换当前草稿。</p><button type="button" disabled={pending} onClick={acceptCurrent}>确认替换草稿</button></>}</div>}
    <footer><button type="button" disabled={pending} onClick={onClose}>关闭</button><button className="reminder-primary" type="submit" disabled={pending || !title.trim()}>保存事务</button></footer>
  </form>;
}
