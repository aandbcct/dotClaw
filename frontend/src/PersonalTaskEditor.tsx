import { useState, type FormEvent } from "react";
import { ApiError } from "./api";
import type { PersonalTask, PersonalTaskInput, TaskDeadline } from "./types";

export function PersonalTaskEditor({ task, timezone, pending, onSave, onReload, onClose }: { task: PersonalTask | null; timezone: string; pending: boolean; onSave: (input: PersonalTaskInput, version?: number) => Promise<void>; onReload: (id: string) => Promise<PersonalTask>; onClose: () => void }) {
  const [title, setTitle] = useState(task?.title || ""); const [description, setDescription] = useState(task?.description || "");
  const [kind, setKind] = useState(task?.deadline?.kind || "none"); const [deadline, setDeadline] = useState(task?.deadline?.value || ""); const [error, setError] = useState("");
  const [version, setVersion] = useState(task?.version); const [conflict, setConflict] = useState(false); const [candidate, setCandidate] = useState<PersonalTask | null>(null);
  async function submit(event: FormEvent) { event.preventDefault(); setError(""); const value: TaskDeadline | null = kind === "none" ? null : { kind, value: deadline } as TaskDeadline;
    try { await onSave({ title: title.trim(), description, deadline: value }, version); } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); setConflict(reason instanceof ApiError && reason.code === "personal_task_version_conflict"); } }
  async function loadCurrent() { if (!task) return; try { setCandidate(await onReload(task.id)); setError(""); } catch (reason) { setError(reason instanceof Error ? reason.message : "重新读取失败"); } }
  function acceptCurrent() { if (!candidate) return; setTitle(candidate.title); setDescription(candidate.description); setKind(candidate.deadline?.kind || "none"); setDeadline(candidate.deadline?.value || ""); setVersion(candidate.version); setCandidate(null); setConflict(false); setError(""); }
  return <form className="task-editor" onSubmit={(event) => void submit(event)}><h2>{task ? "编辑事务" : "新建事务"}</h2>
    <label>标题<input required disabled={pending} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
    <label>说明<textarea disabled={pending} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
    <label>截止类型<select disabled={pending} value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="none">无截止</option><option value="date">日期</option><option value="datetime">具体时刻</option></select></label>
    {kind !== "none" && <label>{kind === "date" ? "截止日期" : `截止时刻（含时区；应用时区 ${timezone}）`}<input disabled={pending} required type={kind === "date" ? "date" : "text"} placeholder="2026-11-20T15:00:00+08:00" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></label>}
    {task?.deadline && (kind === "none" || kind !== task.deadline.kind) && <p className="muted">保存后，依赖原截止类型的活动提醒会取消。</p>}
    {error && <p role="alert">{error}</p>}{conflict && <div><button type="button" disabled={pending} onClick={() => void loadCurrent()}>重新读取</button>{candidate && <><p>重新读取的版本为 {candidate.version}。确认后会替换当前草稿。</p><button type="button" disabled={pending} onClick={acceptCurrent}>确认替换草稿</button></>}</div>}
    <footer><button type="button" disabled={pending} onClick={onClose}>关闭</button><button type="submit" disabled={pending || !title.trim()}>保存</button></footer>
  </form>;
}
