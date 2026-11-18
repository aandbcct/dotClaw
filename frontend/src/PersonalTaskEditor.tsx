import { useState, type FormEvent } from "react";
import type { PersonalTask, PersonalTaskInput, TaskDeadline } from "./types";

export function PersonalTaskEditor({ task, timezone, pending, onSave, onClose }: { task: PersonalTask | null; timezone: string; pending: boolean; onSave: (input: PersonalTaskInput, version?: number) => Promise<void>; onClose: () => void }) {
  const [title, setTitle] = useState(task?.title || ""); const [description, setDescription] = useState(task?.description || "");
  const [kind, setKind] = useState(task?.deadline?.kind || "none"); const [deadline, setDeadline] = useState(task?.deadline?.value || ""); const [error, setError] = useState("");
  async function submit(event: FormEvent) { event.preventDefault(); setError(""); const value: TaskDeadline | null = kind === "none" ? null : { kind, value: deadline } as TaskDeadline;
    try { await onSave({ title: title.trim(), description, deadline: value }, task?.version); } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); } }
  return <form className="task-editor" onSubmit={(event) => void submit(event)}><h2>{task ? "编辑事务" : "新建事务"}</h2>
    <label>标题<input required value={title} onChange={(event) => setTitle(event.target.value)} /></label>
    <label>说明<textarea value={description} onChange={(event) => setDescription(event.target.value)} /></label>
    <label>截止类型<select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="none">无截止</option><option value="date">日期</option><option value="datetime">具体时刻</option></select></label>
    {kind !== "none" && <label>{kind === "date" ? "截止日期" : `截止时刻（含时区；应用时区 ${timezone}）`}<input required type={kind === "date" ? "date" : "text"} placeholder="2026-11-20T15:00:00+08:00" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></label>}
    {error && <p role="alert">{error}</p>}<footer><button type="button" disabled={pending} onClick={onClose}>关闭</button><button type="submit" disabled={pending || !title.trim()}>保存</button></footer>
  </form>;
}
