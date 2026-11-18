import { useState } from "react";
import { getPersonalTask } from "./api";
import { Dialog } from "./Dialog";
import { PersonalTaskEditor } from "./PersonalTaskEditor";
import { usePersonalTasks } from "./usePersonalTasks";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskStatus } from "./types";

const labels: Record<PersonalTaskStatus, string> = { pending: "待办", in_progress: "进行中", completed: "已完成", cancelled: "已取消" };
const next: Partial<Record<PersonalTaskStatus, PersonalTaskStatus[]>> = { pending: ["in_progress", "completed", "cancelled"], in_progress: ["pending", "completed", "cancelled"], completed: ["pending"], cancelled: ["pending"] };
export function PersonalTasks({ onOpenNavigation }: { onOpenNavigation: () => void }) {
  const tasks = usePersonalTasks(); const [editing, setEditing] = useState<PersonalTask | null | undefined>(undefined); const [deleting, setDeleting] = useState<PersonalTask | null>(null); const [actionError, setActionError] = useState("");
  async function save(input: PersonalTaskInput, version?: number) { if (editing && version) await tasks.update(editing.id, input, version); else await tasks.create(input); setEditing(undefined); }
  return <section className="personal-tasks"><header><button className="menu-button" onClick={onOpenNavigation}>☰</button><h1>事务</h1><span>应用时区：{tasks.timezone}</span><button onClick={() => void tasks.reload()}>刷新</button><button onClick={() => setEditing(null)}>新建</button></header>
    <nav aria-label="事务筛选">{(["active", "all", "completed", "cancelled"] as PersonalTaskFilter[]).map((value) => <button key={value} aria-pressed={tasks.filter === value} onClick={() => tasks.setFilter(value)}>{({ active: "未完成", all: "全部", completed: "已完成", cancelled: "已取消" })[value]}</button>)}</nav>
    {tasks.error && <p role="alert">{tasks.error}</p>}{!deleting && actionError && <p role="alert">{actionError}</p>}{tasks.loading ? <p>正在读取事务…</p> : !tasks.items.length ? <p>暂无事务</p> : <ul>{tasks.items.map((task) => <li key={task.id}><strong>{task.title}</strong><span>{labels[task.status]}</span>{task.deadline && <span>{task.deadline.kind === "date" ? task.deadline.value : new Date(task.deadline.value).toLocaleString("zh-CN", { timeZone: tasks.timezone })}</span>}{task.is_overdue && <b>逾期</b>}<p>{task.description}</p><button disabled={tasks.pending} onClick={() => setEditing(task)}>编辑</button>{(next[task.status] || []).map((status) => <button key={status} disabled={tasks.pending} onClick={() => { setActionError(""); void tasks.changeStatus(task, status).catch((error) => setActionError(error instanceof Error ? error.message : "状态更新失败")); }}>{labels[status]}</button>)}<button disabled={tasks.pending} onClick={() => { setActionError(""); setDeleting(task); }}>删除</button></li>)}</ul>}
    {editing !== undefined && <Dialog title={editing ? "编辑事务" : "新建事务"} onClose={() => { if (!tasks.pending) setEditing(undefined); }}><PersonalTaskEditor task={editing} timezone={tasks.timezone} pending={tasks.pending} onSave={save} onReload={getPersonalTask} onClose={() => setEditing(undefined)} /></Dialog>}
    {deleting && <Dialog title="删除事务" onClose={() => { if (!tasks.pending) { setActionError(""); setDeleting(null); } }}><p>永久删除“{deleting.title}”，无法恢复。</p>{actionError && <p role="alert">{actionError}</p>}<footer><button disabled={tasks.pending} onClick={() => { setActionError(""); setDeleting(null); }}>取消</button><button disabled={tasks.pending} onClick={() => { setActionError(""); void tasks.remove(deleting).then(() => setDeleting(null)).catch((error) => setActionError(error instanceof Error ? error.message : "删除失败")); }}>确认删除</button></footer></Dialog>}
  </section>;
}
