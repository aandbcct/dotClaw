import { useEffect, useState } from "react";
import "./reminder-workspace.css";
import { getNotification, getPersonalTask, listTaskReminders } from "./api";
import { Dialog } from "./Dialog";
import { PersonalTaskEditor } from "./PersonalTaskEditor";
import { usePersonalTasks } from "./usePersonalTasks";
import { TaskReminders } from "./TaskReminders";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskStatus, Reminder } from "./types";

const labels: Record<PersonalTaskStatus, string> = { pending: "待办", in_progress: "进行中", completed: "已完成", cancelled: "已取消" };
const next: Partial<Record<PersonalTaskStatus, PersonalTaskStatus[]>> = { pending: ["in_progress", "completed", "cancelled"], in_progress: ["pending", "completed", "cancelled"], completed: ["pending"], cancelled: ["pending"] };
export function PersonalTasks({ onOpenNavigation, focusTaskId, focusNotificationId, onFocusHandled }: { onOpenNavigation: () => void; focusTaskId?: string | null; focusNotificationId?: number | null; onFocusHandled?: () => void }) {
  const tasks = usePersonalTasks(); const [editing, setEditing] = useState<PersonalTask | null | undefined>(undefined); const [deleting, setDeleting] = useState<PersonalTask | null>(null); const [actionError, setActionError] = useState(""); const [reminding, setReminding] = useState<PersonalTask | null>(null);
  const [summaryIds, setSummaryIds] = useState<string[] | null>(null);
  const [missingTask, setMissingTask] = useState(false);
  const [deadlineChanges, setDeadlineChanges] = useState<Reminder[]>([]);
  const [deadlineChangeError, setDeadlineChangeError] = useState("");
  const [taskSuccess, setTaskSuccess] = useState("");
  const visibleTasks = summaryIds ? tasks.items.filter((task) => summaryIds.includes(task.id)) : tasks.items;
  useEffect(() => { if ((!focusTaskId && !focusNotificationId) || tasks.loading) return; tasks.setFilter("all"); setActionError(""); setMissingTask(false); if (focusTaskId) setSummaryIds(null); if (focusNotificationId) { let current = true; setSummaryIds(null); void getNotification(focusNotificationId).then((item) => { if (current) { setSummaryIds(item.members.map((member) => member.task_id)); onFocusHandled?.(); } }).catch((error) => { if (current) { setActionError(error instanceof Error ? error.message : "无法读取摘要关联事务"); onFocusHandled?.(); } }); return () => { current = false; }; } }, [focusTaskId, focusNotificationId, tasks.loading]);
  useEffect(() => { if (!focusTaskId || tasks.loading || tasks.filter !== "all" || !tasks.items.some((task) => task.id === focusTaskId)) return; const target = document.getElementById(`task-${focusTaskId}`); target?.scrollIntoView({ block: "center" }); target?.focus({ preventScroll: true }); onFocusHandled?.(); }, [focusTaskId, tasks.loading, tasks.filter, tasks.items]);
  useEffect(() => { if (!focusTaskId || tasks.loading || tasks.filter !== "all") return; if (!tasks.items.some((task) => task.id === focusTaskId)) { setMissingTask(true); onFocusHandled?.(); } }, [focusTaskId, tasks.loading, tasks.filter, tasks.items]);
  async function save(input: PersonalTaskInput, version?: number) {
    setDeadlineChanges([]); setDeadlineChangeError(""); setTaskSuccess("");
    if (editing && version) {
      const before = await listTaskReminders(editing.id);
      const savedTask = await tasks.update(editing.id, input, version);
      setEditing(undefined);
      setTaskSuccess("事务已保存");
      try {
        const after = await listTaskReminders(editing.id);
        const previous = new Map(before.items.map((item) => [item.id, item]));
        setDeadlineChanges(after.items.filter((item) => {
          const old = previous.get(item.id);
          return Boolean(old && item.version > old.version && Date.parse(item.updated_at) === Date.parse(savedTask.updated_at) && ((item.status === "paused" && item.reason === "deadline_moved_past") || (item.status === "cancelled" && item.reason === "deadline_removed_or_changed_kind")));
        }));
      } catch (reason) { setDeadlineChangeError(reason instanceof Error ? `事务已保存，但读取关联提醒变化失败：${reason.message}` : "事务已保存，但读取关联提醒变化失败"); }
    } else { const created = await tasks.create(input); setReminding(created); setEditing(undefined); setTaskSuccess("事务已创建"); }
  }
  return <section className="personal-tasks"><header><button className="menu-button" onClick={onOpenNavigation}>☰</button><h1>事务</h1><span>应用时区：{tasks.timezone}</span><button onClick={() => void tasks.reload()}>刷新</button><button onClick={() => setEditing(null)}>新建</button></header>
    <nav aria-label="事务筛选">{(["active", "all", "completed", "cancelled"] as PersonalTaskFilter[]).map((value) => <button key={value} aria-pressed={tasks.filter === value} onClick={() => tasks.setFilter(value)}>{({ active: "未完成", all: "全部", completed: "已完成", cancelled: "已取消" })[value]}</button>)}</nav>
    {tasks.error && <p role="alert">{tasks.error}</p>}
    {missingTask && <p role="status">事务已删除 <button onClick={() => setMissingTask(false)}>清除定位</button></p>}
    {!deleting && actionError && <p role="alert">{actionError}</p>}
    {taskSuccess && <p role="status" className="reminder-success">{taskSuccess}</p>}
    {deadlineChangeError && <p role="alert">{deadlineChangeError}</p>}
    {deadlineChanges.length > 0 && <aside role="status"><strong>本次保存已更新关联提醒</strong><ul>{deadlineChanges.map((item) => <li key={item.id}>{item.rule.kind}：{item.reason === "deadline_moved_past" ? "提醒时刻已过去，已暂停" : "截止删除或类型改变，已取消"}</li>)}</ul></aside>}
    {summaryIds && <div className="task-focus-heading"><h2>摘要关联事务</h2><button onClick={() => { setSummaryIds(null); setMissingTask(false); }}>清除定位</button></div>}
    {tasks.loading ? <p>正在读取事务…</p> : !visibleTasks.length ? <p>{summaryIds ? "摘要关联事务均已删除" : "暂无事务"}</p> : <ul className="task-list">{visibleTasks.map((task) => <li className="task-row" tabIndex={-1} id={`task-${task.id}`} key={task.id}>
      <div className="task-row-heading"><strong>{task.title}</strong><span className={`task-status task-status--${task.status}`}>{labels[task.status]}</span>{task.deadline && <span>{task.deadline.kind === "date" ? task.deadline.value : new Date(task.deadline.value).toLocaleString("zh-CN", { timeZone: tasks.timezone })}</span>}{task.is_overdue && <b>逾期</b>}</div>
      {task.description && <p>{task.description}</p>}
      <div className="task-row-actions"><button disabled={tasks.pending} onClick={() => setEditing(task)}>编辑</button><button onClick={() => setReminding(task)}>提醒</button>{(next[task.status] || []).map((status) => <button key={status} disabled={tasks.pending} onClick={() => { setActionError(""); void tasks.changeStatus(task, status).catch((error) => setActionError(error instanceof Error ? error.message : "状态更新失败")); }}>{labels[status]}</button>)}<button disabled={tasks.pending} onClick={() => { setActionError(""); setDeleting(task); }}>删除</button></div>
    </li>)}</ul>}
    {editing !== undefined && <Dialog title={editing ? "编辑事务" : "新建事务"} onClose={() => { if (!tasks.pending) setEditing(undefined); }}><PersonalTaskEditor task={editing} timezone={tasks.timezone} pending={tasks.pending} onSave={save} onReload={getPersonalTask} onClose={() => setEditing(undefined)} /></Dialog>}
    {deleting && <Dialog title="删除事务" onClose={() => { if (!tasks.pending) { setActionError(""); setDeleting(null); } }}><p>永久删除“{deleting.title}”及其提醒和关联通知，无法恢复。</p>{actionError && <p role="alert">{actionError}</p>}<footer><button disabled={tasks.pending} onClick={() => { setActionError(""); setDeleting(null); }}>取消</button><button disabled={tasks.pending} onClick={() => { setActionError(""); void tasks.remove(deleting).then(() => setDeleting(null)).catch((error) => setActionError(error instanceof Error ? error.message : "删除失败")); }}>确认删除</button></footer></Dialog>}
    {reminding && <TaskReminders task={reminding} onClose={() => setReminding(null)} />}
  </section>;
}
