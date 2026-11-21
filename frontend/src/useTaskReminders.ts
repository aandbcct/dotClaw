import { useCallback, useEffect, useRef, useState } from "react";
import { changeTaskReminderStatus, createTaskReminder, getPersonalTask, listTaskReminders, updateTaskReminder } from "./api";
import type { PersonalTask, Reminder, ReminderRule } from "./types";

export function useTaskReminders(taskId: string) {
  const [task, setTask] = useState<PersonalTask | null>(null);
  const [items, setItems] = useState<Reminder[]>([]);
  const [timezone, setTimezone] = useState("");
  const [schedulerEnabled, setSchedulerEnabled] = useState(true);
  const [scanLag, setScanLag] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const lock = useRef(false);
  const reload = useCallback(async (throwOnError = false): Promise<Reminder[] | null> => {
    const controller = new AbortController();
    try {
      const [current, reminders] = await Promise.all([getPersonalTask(taskId), listTaskReminders(taskId, controller.signal)]);
      setTask(current); setItems(current.reminders); setTimezone(reminders.timezone); setSchedulerEnabled(reminders.scheduler_enabled); setScanLag(reminders.scan_lag_seconds); setError("");
      return current.reminders;
    } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "读取提醒失败"); if (throwOnError) throw reason; return null; }
    finally { setLoading(false); }
  }, [taskId]);
  useEffect(() => { void reload(); }, [reload]);
  const mutate = useCallback(async (operation: () => Promise<unknown>, successMessage: string, propagate = false) => {
    if (lock.current) {
      const reason = new Error("请等待当前操作完成");
      setError(reason.message);
      if (propagate) throw reason;
      return;
    }
    lock.current = true; setPending(true); setError(""); setSuccess("");
    try { await operation(); await reload(); setSuccess(successMessage); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "提醒操作失败"); if (propagate) throw reason; }
    finally { lock.current = false; setPending(false); }
  }, [reload]);
  return { task, items, timezone, schedulerEnabled, scanLag, loading, pending, error, success, reload,
    create: (rule: ReminderRule) => mutate(() => createTaskReminder(taskId, rule, task!.version), "提醒已新增", true),
    update: (item: Reminder, rule: ReminderRule) => mutate(() => updateTaskReminder(taskId, item.id, rule, item.version, task!.version), "提醒已保存", true),
    changeStatus: (item: Reminder, action: "pause" | "resume" | "cancel") => mutate(() => changeTaskReminderStatus(taskId, item.id, action, item.version, task!.version), action === "pause" ? "提醒已暂停" : action === "resume" ? "提醒已恢复" : "提醒已取消", true),
  };
}
