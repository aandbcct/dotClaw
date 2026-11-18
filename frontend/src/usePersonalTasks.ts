import { useCallback, useEffect, useRef, useState } from "react";
import { changePersonalTaskStatus, createPersonalTask, deletePersonalTask, listPersonalTasks, updatePersonalTask } from "./api";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskStatus } from "./types";

export function usePersonalTasks() {
  const [items, setItems] = useState<PersonalTask[]>([]); const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [filter, setFilter] = useState<PersonalTaskFilter>("active"); const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false); const [error, setError] = useState("");
  const sequence = useRef(0); const controller = useRef<AbortController | null>(null); const mutation = useRef(false);
  const reload = useCallback(async () => {
    const current = ++sequence.current; controller.current?.abort(); const abort = new AbortController(); controller.current = abort; setLoading(true);
    try { const result = await listPersonalTasks(filter, abort.signal); if (current === sequence.current) { setItems(result.items); setTimezone(result.timezone); setError(""); } }
    catch (reason) { if ((reason as Error).name !== "AbortError" && current === sequence.current) setError("事务列表读取失败，请重试"); }
    finally { if (current === sequence.current) setLoading(false); }
  }, [filter]);
  useEffect(() => { void reload(); return () => { controller.current?.abort(); }; }, [reload]);
  const run = useCallback(async <T,>(action: () => Promise<T>): Promise<T> => {
    if (mutation.current) throw new Error("请等待当前操作完成"); mutation.current = true; setPending(true); setError(""); ++sequence.current;
    try { const result = await action(); await reload(); return result; }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); throw reason; }
    finally { mutation.current = false; setPending(false); }
  }, [reload]);
  return { items, timezone, filter, loading, pending, error, setFilter, reload,
    create: (input: PersonalTaskInput) => run(() => createPersonalTask(input)),
    update: (id: string, input: PersonalTaskInput, version: number) => run(() => updatePersonalTask(id, input, version)),
    changeStatus: (task: PersonalTask, status: PersonalTaskStatus) => run(() => changePersonalTaskStatus(task.id, status, task.version)),
    remove: (task: PersonalTask) => run(() => deletePersonalTask(task.id, task.version)) };
}
