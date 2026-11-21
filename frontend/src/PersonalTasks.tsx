import { useEffect, useState } from "react";
import "./reminder-workspace.css";
import { ApiError, getNotification, getPersonalTask } from "./api";
import { Dialog } from "./Dialog";
import { PersonalTaskEditor } from "./PersonalTaskEditor";
import { usePersonalTasks } from "./usePersonalTasks";
import { TaskReminders } from "./TaskReminders";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskStatus, ReminderChange, ReminderRule } from "./types";

const labels: Record<PersonalTaskStatus, string> = { pending: "待办", in_progress: "进行中", completed: "已完成", cancelled: "已取消" };
const next: Partial<Record<PersonalTaskStatus, PersonalTaskStatus[]>> = { pending: ["in_progress", "completed", "cancelled"], in_progress: ["pending", "completed", "cancelled"], completed: ["pending"], cancelled: ["pending"] };
function reminderEffectText(changes: ReminderChange[]): string {
  const counts = { rescheduled: 0, paused: 0, cancelled: 0 };
  changes.forEach((change) => { counts[change.action] += 1; });
  return ([counts.rescheduled ? `改期 ${counts.rescheduled} 条` : "", counts.paused ? `暂停 ${counts.paused} 条` : "", counts.cancelled ? `取消 ${counts.cancelled} 条` : ""]
    .filter(Boolean).join("、"));
}
export function PersonalTasks({ onOpenNavigation, focusTaskId, focusNotificationId, onFocusHandled }: { onOpenNavigation: () => void; focusTaskId?: string | null; focusNotificationId?: number | null; onFocusHandled?: () => void }) {
  const tasks = usePersonalTasks(); const [editing, setEditing] = useState<PersonalTask | null | undefined>(undefined); const [deleting, setDeleting] = useState<PersonalTask | null>(null); const [actionError, setActionError] = useState(""); const [reminding, setReminding] = useState<PersonalTask | null>(null);
  const [focusResult, setFocusResult] = useState<{ kind: "task" | "summary"; items: PersonalTask[]; missingIds: string[] } | null>(null);
  const [focusError, setFocusError] = useState("");
  const [focusLoading, setFocusLoading] = useState(false);
  const [focusRetry, setFocusRetry] = useState(0);
  const [focusTarget, setFocusTarget] = useState<{ taskId?: string; notificationId?: number } | null>(null);
  const [keywordDraft, setKeywordDraft] = useState("");
  const [reminderChanges, setReminderChanges] = useState<ReminderChange[]>([]);
  const [taskSuccess, setTaskSuccess] = useState("");
  const visibleTasks = tasks.items;
  useEffect(() => { setKeywordDraft(tasks.keyword); }, [tasks.keyword]);
  useEffect(() => { if (focusTaskId || focusNotificationId) setFocusTarget({ taskId: focusTaskId || undefined, notificationId: focusNotificationId || undefined }); }, [focusTaskId, focusNotificationId]);
  useEffect(() => {
    if (!focusTarget) return;
    let active = true;
    const kind = focusTarget.notificationId ? "summary" : "task";
    setFocusResult(null); setFocusError(""); setFocusLoading(true);
    void (async () => {
      try {
        const ids = focusTarget.notificationId
          ? [...new Set((await getNotification(focusTarget.notificationId)).members.map((member) => member.task_id))]
          : [focusTarget.taskId!];
        const results = await Promise.allSettled(ids.map((id) => getPersonalTask(id)));
        const items: PersonalTask[] = []; const missingIds: string[] = []; let failures = 0;
        results.forEach((result, index) => {
          if (result.status === "fulfilled") items.push(result.value);
          else if (result.reason instanceof ApiError && result.reason.status === 404) missingIds.push(ids[index]);
          else failures += 1;
        });
        if (active) {
          setFocusResult({ kind, items, missingIds });
          setFocusError(failures ? "部分关联事务读取失败，请重试。" : "");
        }
      } catch (error) {
        if (active) {
          setFocusResult({ kind, items: [], missingIds: [] });
          setFocusError(error instanceof Error ? `无法读取关联事务：${error.message}` : "无法读取关联事务，请重试。");
        }
      } finally {
        if (active) { setFocusLoading(false); onFocusHandled?.(); }
      }
    })();
    return () => { active = false; };
  }, [focusTarget, focusRetry]);
  async function save(input: PersonalTaskInput, version?: number, initialReminder?: ReminderRule) {
    setReminderChanges([]); setTaskSuccess("");
    if (editing && version) {
      const savedTask = await tasks.update(editing.id, input, version);
      setEditing(undefined);
      setReminderChanges(savedTask.reminder_changes || []);
      const effects = reminderEffectText(savedTask.reminder_changes || []);
      setTaskSuccess(effects ? `事务已保存 · ${effects}` : "事务已保存");
    } else { await tasks.create(input, initialReminder ? [initialReminder] : []); setEditing(undefined); setTaskSuccess(initialReminder ? "事务及提醒已创建" : "事务已创建"); }
  }
  function changeTaskStatus(task: PersonalTask, status: PersonalTaskStatus) {
    setActionError("");
    setTaskSuccess("");
    const success = status === "completed" ? "事务已完成" : status === "pending" ? "事务已重新打开" : status === "in_progress" ? "事务已开始" : "事务已取消";
    void tasks.changeStatus(task, status).then((saved) => {
      const changes = saved.reminder_changes || []; setReminderChanges(changes);
      const effects = reminderEffectText(changes); setTaskSuccess(effects ? `${success} · ${effects}` : success);
    }).catch((error) => setActionError(error instanceof Error ? error.message : "状态更新失败"));
  }
  return <section className="personal-tasks"><header><button className="menu-button" onClick={onOpenNavigation}>☰</button><h1>事务</h1><span>应用时区：{tasks.timezone}</span><button onClick={() => void tasks.reload(true)}>刷新</button><button onClick={() => setEditing(null)}>新建</button></header>
    <nav aria-label="事务筛选">{(["active", "all", "completed", "cancelled"] as PersonalTaskFilter[]).map((value) => <button key={value} aria-pressed={tasks.filter === value} onClick={() => tasks.setFilter(value)}>{({ active: "未完成", all: "全部", completed: "已完成", cancelled: "已取消" })[value]}</button>)}</nav>
    <form className="task-query-filters" onSubmit={(event) => { event.preventDefault(); tasks.setKeyword(keywordDraft); }}>
      <label>关键词<input aria-label="事务关键词" value={keywordDraft} onChange={(event) => setKeywordDraft(event.target.value)} /></label>
      <button type="submit">搜索</button>
      <label>截止日期从<input aria-label="截止日期从" type="date" value={tasks.deadlineFrom || ""} onChange={(event) => tasks.setDeadlineFrom(event.target.value || null)} /></label>
      <label>截止日期至（不含）<input aria-label="截止日期至" type="date" value={tasks.deadlineUntil || ""} onChange={(event) => tasks.setDeadlineUntil(event.target.value || null)} /></label>
      <label><input aria-label="仅逾期" type="checkbox" checked={tasks.overdueOnly} onChange={(event) => tasks.setOverdueOnly(event.target.checked)} />仅逾期</label>
    </form>
    {tasks.error && <p role="alert">{tasks.error} <button disabled={tasks.pending} onClick={() => void tasks.reload(true)}>重试</button></p>}
    {!deleting && actionError && <p role="alert">{actionError}</p>}
    {taskSuccess && <p role="status" className="reminder-success">{taskSuccess}</p>}
    {focusResult && <section className="task-focus-results" aria-label={focusResult.kind === "summary" ? "摘要关联事务" : "定位事务"}>
      <header><h2>{focusResult.kind === "summary" ? "摘要关联事务" : "定位事务"}</h2><button onClick={() => { setFocusResult(null); setFocusTarget(null); }}>清除定位</button></header>
      {focusLoading && <p>正在读取关联事务…</p>}
      {focusError && <p role="alert">{focusError} <button onClick={() => setFocusRetry((value) => value + 1)}>重试</button></p>}
      {focusResult.items.map((task) => <p key={task.id}><strong>{task.title}</strong> · {labels[task.status]}{task.is_overdue ? " · 逾期" : ""}</p>)}
      {focusResult.missingIds.map((id) => <p key={id} role="status">关联事务已删除（{id}）</p>)}
      {!focusLoading && !focusError && !focusResult.items.length && !focusResult.missingIds.length && <p>没有可显示的关联事务</p>}
    </section>}
    {tasks.loading ? <p>正在读取事务…</p> : !visibleTasks.length ? <p>暂无事务</p> : <ul className="task-list">{visibleTasks.map((task) => {
      const summary = task.reminder_summary;
      const primaryStatus = task.status === "pending" || task.status === "in_progress" ? "completed" : "pending";
      return <li className="task-row" tabIndex={-1} id={`task-${task.id}`} key={task.id}>
        <div className="task-row-heading"><strong>{task.title}</strong><span className={`task-status task-status--${task.status}`}>{labels[task.status]}</span>{task.deadline && <span>{task.deadline.kind === "date" ? task.deadline.value : new Date(task.deadline.value).toLocaleString("zh-CN", { timeZone: tasks.timezone })}</span>}{task.is_overdue && <b>逾期</b>}</div>
        {task.description && <p>{task.description}</p>}
        <div className="task-reminder-summary" aria-live="polite">
          {summary.active_count > 0 && summary.next_fire_at ? <span>下次提醒：{new Date(summary.next_fire_at).toLocaleString("zh-CN", { timeZone: tasks.timezone })} · {tasks.timezone}{summary.active_count > 1 ? `（另有 ${summary.active_count - 1} 条）` : ""}</span> : <span>无待触发提醒</span>}
        </div>
        <div className="task-row-actions">
          <button className="task-primary-action" disabled={tasks.pending} onClick={() => changeTaskStatus(task, primaryStatus)}>{primaryStatus === "completed" ? "完成" : "重新打开"}</button>
          <button onClick={() => setReminding(task)}>{task.status === "pending" || task.status === "in_progress" ? "提醒" : "查看提醒"}</button>
          <details className="task-more"><summary>更多</summary><div>
            <button disabled={tasks.pending} onClick={() => setEditing(task)}>编辑事务</button>
            {(next[task.status] || []).filter((status) => status !== primaryStatus).map((status) => <button key={status} disabled={tasks.pending} onClick={() => changeTaskStatus(task, status)}>{labels[status]}</button>)}
            <button disabled={tasks.pending} onClick={() => { setActionError(""); setDeleting(task); }}>删除</button>
          </div></details>
        </div>
      </li>;
    })}</ul>}
    {!tasks.loading && tasks.hasMore && <button disabled={tasks.loadingMore || tasks.pending} onClick={() => void tasks.loadMore()}>{tasks.loadingMore ? "正在加载…" : "加载更多"}</button>}
    {editing !== undefined && <Dialog title={editing ? "编辑事务" : "新建事务"} onClose={() => { if (!tasks.pending) setEditing(undefined); }}><PersonalTaskEditor task={editing} timezone={tasks.timezone} pending={tasks.pending} onSave={save} onReload={getPersonalTask} onClose={() => setEditing(undefined)} /></Dialog>}
    {deleting && <Dialog title="删除事务" onClose={() => { if (!tasks.pending) { setActionError(""); setDeleting(null); } }}><p>永久删除“{deleting.title}”及其提醒和关联通知，无法恢复。</p>{actionError && <p role="alert">{actionError}</p>}<footer><button disabled={tasks.pending} onClick={() => { setActionError(""); setDeleting(null); }}>取消</button><button disabled={tasks.pending} onClick={() => { setActionError(""); void tasks.remove(deleting).then(() => setDeleting(null)).catch((error) => setActionError(error instanceof Error ? error.message : "删除失败")); }}>确认删除</button></footer></Dialog>}
    {reminding && <TaskReminders task={reminding} onClose={() => setReminding(null)} onChanged={() => void tasks.reload(true)} />}
  </section>;
}
