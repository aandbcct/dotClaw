import { useState } from "react";
import { Dialog } from "./Dialog";
import { ReminderEditor } from "./ReminderEditor";
import { useTaskReminders } from "./useTaskReminders";
import type { PersonalTask, Reminder } from "./types";

const reasons: Record<string, string> = { deadline_moved_past: "截止改期后提醒时刻已过去，已暂停", deadline_removed_or_changed_kind: "截止已删除或类型变化，提醒已取消", invalid_local_time: "该当地时刻不存在，已暂停" };
export function TaskReminders({ task, onClose }: { task: PersonalTask; onClose: () => void }) {
  const state = useTaskReminders(task.id);
  const [editing, setEditing] = useState<Reminder | null | undefined>(undefined);
  return <Dialog title={`“${task.title}”的提醒`} onClose={() => { if (!state.pending) onClose(); }}>
    {state.loading ? <p>正在读取提醒…</p> : <>
      <p>规则时区：{state.timezone} · {state.schedulerEnabled ? state.scanLag !== null && state.scanLag > 30 ? "提醒调度延迟" : "调度运行中" : "提醒调度已关闭"}</p>
      {state.error && <p role="alert">{state.error}</p>}
      <ul>{state.items.map((item) => <li key={item.id}><strong>{item.rule.kind}</strong><span>{item.status}</span>{item.next_fire_at && <time>{new Date(item.next_fire_at).toLocaleString("zh-CN", { timeZone: item.timezone })}</time>}{item.reason && <p>{reasons[item.reason] || item.reason}</p>}{item.status === "active" && <button disabled={state.pending || item.rule.kind === "at" || item.rule.kind === "before_deadline" || item.rule.kind === "before_date"} onClick={() => void state.changeStatus(item, "pause")}>暂停</button>}{item.status === "paused" && <button disabled={state.pending} onClick={() => void state.changeStatus(item, "resume")}>恢复</button>}{["active","paused"].includes(item.status) && <><button disabled={state.pending} onClick={() => setEditing(item)}>修改</button><button disabled={state.pending} onClick={() => void state.changeStatus(item, "cancel")}>取消</button></>}</li>)}</ul>
      {task.status === "pending" || task.status === "in_progress" ? <button disabled={state.pending} onClick={() => setEditing(null)}>新增提醒</button> : <p>事务已结束，只能查看提醒历史。</p>}
    </>}
    {editing !== undefined && state.task && <Dialog title={editing ? "修改提醒" : "新增提醒"} onClose={() => setEditing(undefined)}><ReminderEditor task={state.task} reminder={editing} timezone={state.timezone} pending={state.pending} onReload={editing ? async () => (await state.reload(true))?.find((item) => item.id === editing.id) || null : undefined} onSave={(rule, baseReminder) => editing ? state.update(baseReminder || editing, rule) : state.create(rule)} onClose={() => setEditing(undefined)} /></Dialog>}
  </Dialog>;
}
