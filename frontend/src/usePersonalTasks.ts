import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, changePersonalTaskStatus, createPersonalTask, deletePersonalTask, listPersonalTasks, updatePersonalTask } from "./api";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskStatus } from "./types";

export function usePersonalTasks() {
  const [items, setItems] = useState<PersonalTask[]>([]); const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [filter, setFilter] = useState<PersonalTaskFilter>("active"); const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false); const [error, setError] = useState("");
  const loadingRef = useRef(loading); loadingRef.current = loading;
  const sequence = useRef(0); const controller = useRef<AbortController | null>(null); const mutation = useRef(false);
  const filterRef = useRef(filter); filterRef.current = filter;
  const reload = useCallback(async () => {
    const current = ++sequence.current; controller.current?.abort(); const abort = new AbortController(); controller.current = abort; setLoading(true);
    try { const result = await listPersonalTasks(filterRef.current, abort.signal); if (current === sequence.current) { setItems(result.items); setTimezone(result.timezone); setError(""); } return true; }
    catch (reason) { if ((reason as Error).name === "AbortError" || current !== sequence.current) return true; setError("事务列表读取失败，请重试"); return false; }
    finally { if (current === sequence.current) setLoading(false); }
  }, []);
  useEffect(() => {
    void reload();
    const onFocus = () => { if (!mutation.current && !loadingRef.current) void reload(); };
    const timer = window.setInterval(onFocus, 60_000);
    window.addEventListener("focus", onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", onFocus); controller.current?.abort(); };
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
  return { items, timezone, filter, loading, pending, error, setFilter, reload,
    create: (input: PersonalTaskInput) => run(() => createPersonalTask(input)),
    update: (id: string, input: PersonalTaskInput, version: number) => run(() => updatePersonalTask(id, input, version)),
    changeStatus: (task: PersonalTask, status: PersonalTaskStatus) => run(() => changePersonalTaskStatus(task.id, status, task.version)),
    remove: (task: PersonalTask) => run(() => deletePersonalTask(task.id, task.version)) };
}
