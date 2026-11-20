import { useState } from "react";
import "./reminder-workspace.css";
import { Dialog } from "./Dialog";
import { ReminderEditor } from "./ReminderEditor";
import { nextReminderLabel, reminderReasonLabel, reminderRuleSummary, reminderStatusLabel } from "./reminderPresentation";
import { useTaskReminders } from "./useTaskReminders";
import type { PersonalTask, Reminder } from "./types";

export function TaskReminders({ task, onClose }: { task: PersonalTask; onClose: () => void }) {
  const state = useTaskReminders(task.id);
  const [editing, setEditing] = useState<Reminder | null | undefined>(undefined);

  return <Dialog title={editing !== undefined ? editing ? "修改提醒" : "新增提醒" : `“${task.title}”的提醒`} onClose={() => { if (!state.pending) onClose(); }}>
    <section className="reminder-workspace">
      {editing !== undefined && state.task ? <ReminderEditor task={state.task} reminder={editing} timezone={state.timezone} pending={state.pending} onReload={editing ? async () => (await state.reload(true))?.find((item) => item.id === editing.id) || null : undefined} onSave={(rule, baseReminder) => editing ? state.update(baseReminder || editing, rule) : state.create(rule)} onClose={() => setEditing(undefined)} /> : <>
        {state.loading ? <p>正在读取提醒…</p> : <>
          <p className="reminder-context">规则时区：<strong>{state.timezone}</strong><span>{state.schedulerEnabled ? state.scanLag !== null && state.scanLag > 30 ? "提醒调度延迟" : "调度运行中" : "提醒调度已关闭"}</span></p>
          {state.success && <p role="status" className="reminder-success">{state.success}</p>}
          {state.error && <p role="alert" className="reminder-error">{state.error}</p>}
          {state.items.length ? <ul className="reminder-list">{state.items.map((item) => <li key={item.id}>
            <div className="reminder-item-main"><strong>{reminderRuleSummary(item.rule, item.timezone)}</strong><span className={`reminder-status reminder-status--${item.status}`}>{reminderStatusLabel(item.status)}</span></div>
            {item.next_fire_at && <time>下次：{nextReminderLabel(item)} · {item.timezone}</time>}
            {reminderReasonLabel(item.reason) && <p className="reminder-note">{reminderReasonLabel(item.reason)}</p>}
            <div className="reminder-actions">
              {item.status === "active" && <button disabled={state.pending || item.rule.kind === "at" || item.rule.kind === "before_deadline" || item.rule.kind === "before_date"} onClick={() => void state.changeStatus(item, "pause")}>暂停</button>}
              {item.status === "paused" && <button disabled={state.pending} onClick={() => void state.changeStatus(item, "resume")}>恢复</button>}
              {(["active", "paused"].includes(item.status)) && <><button disabled={state.pending} onClick={() => setEditing(item)}>修改</button><button disabled={state.pending} onClick={() => void state.changeStatus(item, "cancel")}>取消提醒</button></>}
            </div>
          </li>)}</ul> : !state.loading && <p className="reminder-empty">还没有提醒</p>}
          {task.status === "pending" || task.status === "in_progress" ? <button className="reminder-primary" disabled={state.pending} onClick={() => setEditing(null)}>新增提醒</button> : <p className="reminder-note">事务已结束，只能查看提醒历史。</p>}
        </>}
      </>}
    </section>
  </Dialog>;
}
