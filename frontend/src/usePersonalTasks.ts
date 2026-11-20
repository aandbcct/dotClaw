import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, changePersonalTaskStatus, createPersonalTask, deletePersonalTask, listPersonalTasks, listTaskReminders, updatePersonalTask } from "./api";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskStatus, Reminder, ReminderRule } from "./types";

interface ReminderSummaryState { items: Reminder[]; loading: boolean; error: string; }

export function usePersonalTasks() {
  const [items, setItems] = useState<PersonalTask[]>([]); const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [filter, setFilter] = useState<PersonalTaskFilter>("active"); const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false); const [error, setError] = useState("");
  const [reminderSummaries, setReminderSummaries] = useState<Record<string, ReminderSummaryState>>({});
  const loadingRef = useRef(loading); loadingRef.current = loading;
  const sequence = useRef(0); const controller = useRef<AbortController | null>(null); const mutation = useRef(false);
  const reminderControllers = useRef(new Map<string, AbortController>());
  const reminderSequences = useRef(new Map<string, number>());
  const reminderRequests = useRef(new Set<string>());
  const filterRef = useRef(filter); filterRef.current = filter;
  const reload = useCallback(async () => {
    const current = ++sequence.current; controller.current?.abort(); const abort = new AbortController(); controller.current = abort; setLoading(true);
    try { const result = await listPersonalTasks(filterRef.current, abort.signal); if (current === sequence.current) { setItems(result.items); setTimezone(result.timezone); setError(""); } return true; }
    catch (reason) { if ((reason as Error).name === "AbortError" || current !== sequence.current) return true; setError("事务列表读取失败，请重试"); return false; }
    finally { if (current === sequence.current) setLoading(false); }
  }, []);
  const refreshReminderSummary = useCallback(async (taskId: string) => {
    const current = (reminderSequences.current.get(taskId) || 0) + 1;
    reminderSequences.current.set(taskId, current);
    reminderControllers.current.get(taskId)?.abort();
    const abort = new AbortController();
    reminderControllers.current.set(taskId, abort);
    setReminderSummaries((previous) => ({
      ...previous,
      [taskId]: { items: previous[taskId]?.items || [], loading: true, error: "" },
    }));
    try {
      const result = await listTaskReminders(taskId, abort.signal);
      if (reminderSequences.current.get(taskId) === current) {
        setReminderSummaries((previous) => ({ ...previous, [taskId]: { items: result.items, loading: false, error: "" } }));
      }
    } catch (reason) {
      if (reminderSequences.current.get(taskId) === current && (reason as Error).name !== "AbortError") {
        setReminderSummaries((previous) => ({
          ...previous,
          [taskId]: { items: previous[taskId]?.items || [], loading: false, error: reason instanceof Error ? reason.message : "提醒摘要读取失败" },
        }));
      }
    } finally {
      if (reminderSequences.current.get(taskId) === current) reminderControllers.current.delete(taskId);
    }
  }, []);
  useEffect(() => {
    for (const task of items) {
      if (reminderRequests.current.has(task.id)) continue;
      reminderRequests.current.add(task.id);
      void refreshReminderSummary(task.id);
    }
  }, [items, refreshReminderSummary]);
  useEffect(() => {
    void reload();
    const onFocus = () => { if (!mutation.current && !loadingRef.current) void reload(); };
    const timer = window.setInterval(onFocus, 60_000);
    window.addEventListener("focus", onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", onFocus); controller.current?.abort(); reminderControllers.current.forEach((request) => request.abort()); };
  }, [filter, reload]);
  const run = useCallback(async <T,>(action: () => Promise<T>): Promise<T> => {
    if (mutation.current) throw new Error("请等待当前操作完成"); mutation.current = true; setPending(true); setError(""); ++sequence.current;
    try { const result = await action(); if (!await reload()) setError("操作已保存，列表刷新失败"); return result; }
    catch (reason) {
      if (reason instanceof ApiError) throw reason;
      throw new Error("未能确认保存结果，请刷新核对后再操作", { cause: reason });
    }
    finally { mutation.current = false; setPending(false); }
  }, [reload]);
  return { items, timezone, filter, loading, pending, error, reminderSummaries, refreshReminderSummary, setFilter, reload,
    create: (input: PersonalTaskInput, reminders?: ReminderRule[]) => run(() => createPersonalTask(input, reminders)),
    update: async (id: string, input: PersonalTaskInput, version: number) => { const saved = await run(() => updatePersonalTask(id, input, version)); await refreshReminderSummary(id); return saved; },
    changeStatus: async (task: PersonalTask, status: PersonalTaskStatus) => { const saved = await run(() => changePersonalTaskStatus(task.id, status, task.version)); await refreshReminderSummary(task.id); return saved; },
    remove: async (task: PersonalTask) => { const result = await run(() => deletePersonalTask(task.id, task.version)); reminderSequences.current.set(task.id, (reminderSequences.current.get(task.id) || 0) + 1); reminderControllers.current.get(task.id)?.abort(); reminderControllers.current.delete(task.id); setReminderSummaries((previous) => { const next = { ...previous }; delete next[task.id]; return next; }); return result; } };
}
