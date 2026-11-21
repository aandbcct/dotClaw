import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, changePersonalTaskStatus, createPersonalTask, deletePersonalTask, listPersonalTasks, updatePersonalTask } from "./api";
import type { PersonalTask, PersonalTaskFilter, PersonalTaskInput, PersonalTaskListItem, PersonalTaskQuery, PersonalTaskStatus, ReminderRule } from "./types";

type TaskFilters = Pick<PersonalTaskQuery, "filter" | "keyword" | "deadline_from" | "deadline_until" | "overdue_only">;
const pageSize = 50;

export function usePersonalTasks() {
  const [items, setItems] = useState<PersonalTaskListItem[]>([]);
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [filters, setFiltersState] = useState<TaskFilters>({ filter: "active", keyword: "", deadline_from: null, deadline_until: null, overdue_only: false });
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const queryRef = useRef(filters); queryRef.current = filters;
  const loadedCount = useRef(0);
  const nextOffset = useRef(0);
  const sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const mutation = useRef(false);
  const loadingRef = useRef(loading); loadingRef.current = loading;

  const reload = useCallback(async (preserveLoaded = false): Promise<boolean> => {
    const current = ++sequence.current;
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    setLoading(true); setLoadingMore(false);
    try {
      const query = queryRef.current;
      const first = await listPersonalTasks({ ...query, offset: 0, limit: pageSize }, abort.signal);
      const target = preserveLoaded ? Math.max(pageSize, loadedCount.current) : pageSize;
      const collected = new Map(first.items.map((item) => [item.id, item]));
      let offset = first.items.length;
      let more = first.has_more;
      while (collected.size < target && offset < first.total) {
        const page = await listPersonalTasks({ ...query, offset, limit: pageSize }, abort.signal);
        if (!page.items.length) break;
        offset += page.items.length;
        page.items.forEach((item) => collected.set(item.id, item));
        more = page.has_more;
      }
      if (current === sequence.current) {
        const loaded = [...collected.values()];
        setItems(loaded); loadedCount.current = loaded.length; nextOffset.current = offset;
        setHasMore(more && offset < first.total); setTotal(first.total); setTimezone(first.timezone); setError("");
      }
      return true;
    } catch (reason) {
      if ((reason as Error).name === "AbortError" || current !== sequence.current) return true;
      setError("事务列表读取失败，请重试");
      return false;
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore || loading || !hasMore) return;
    const current = sequence.current;
    const abort = new AbortController(); controller.current?.abort(); controller.current = abort;
    setLoadingMore(true); setError("");
    try {
      const result = await listPersonalTasks({ ...queryRef.current, offset: nextOffset.current, limit: pageSize }, abort.signal);
      if (current === sequence.current) {
        nextOffset.current += result.items.length;
        setItems((previous) => {
          const known = new Set(previous.map((item) => item.id));
          const next = [...previous, ...result.items.filter((item) => !known.has(item.id))];
          loadedCount.current = next.length;
          return next;
        });
        setTotal(result.total); setHasMore(result.has_more); setTimezone(result.timezone);
      }
    } catch (reason) {
      if ((reason as Error).name !== "AbortError" && current === sequence.current) setError("事务列表读取失败，请重试");
    } finally { if (current === sequence.current) setLoadingMore(false); }
  }, [hasMore, loading, loadingMore]);

  const setFilters = useCallback((update: Partial<TaskFilters>) => {
    const next = { ...queryRef.current, ...update };
    if (Object.keys(next).every((key) => next[key as keyof TaskFilters] === queryRef.current[key as keyof TaskFilters])) return;
    sequence.current += 1; controller.current?.abort();
    queryRef.current = next; loadedCount.current = 0; nextOffset.current = 0;
    setItems([]); setTotal(0); setHasMore(false); setError(""); setLoading(true);
    setFiltersState(next);
  }, []);

  useEffect(() => {
    void reload();
    const onFocus = () => { if (!mutation.current && !loadingRef.current) void reload(true); };
    const timer = window.setInterval(onFocus, 60_000);
    window.addEventListener("focus", onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [filters, reload]);
  useEffect(() => () => { sequence.current += 1; controller.current?.abort(); }, []);

  const run = useCallback(async <T,>(action: () => Promise<T>): Promise<T> => {
    if (mutation.current) throw new Error("请等待当前操作完成");
    mutation.current = true; setPending(true); setError(""); sequence.current += 1; controller.current?.abort();
    try {
      const result = await action();
      if (!await reload(true)) setError("操作已保存，列表刷新失败");
      return result;
    } catch (reason) {
      if (reason instanceof ApiError) throw reason;
      throw new Error("未能确认保存结果，请刷新核对后再操作", { cause: reason });
    } finally { mutation.current = false; setPending(false); }
  }, [reload]);

  return { items, timezone, filter: filters.filter, keyword: filters.keyword,
    deadlineFrom: filters.deadline_from, deadlineUntil: filters.deadline_until, overdueOnly: filters.overdue_only,
    total, hasMore, loading, loadingMore, pending, error,
    setFilter: (filter: PersonalTaskFilter) => setFilters({ filter }),
    setKeyword: (keyword: string) => setFilters({ keyword }),
    setDeadlineFrom: (deadline_from: string | null) => setFilters({ deadline_from }),
    setDeadlineUntil: (deadline_until: string | null) => setFilters({ deadline_until }),
    setOverdueOnly: (overdue_only: boolean) => setFilters({ overdue_only }),
    reload, loadMore,
    create: (input: PersonalTaskInput, reminders?: ReminderRule[]) => run(() => createPersonalTask(input, reminders)),
    update: (id: string, input: PersonalTaskInput, version: number) => run(() => updatePersonalTask(id, input, version)),
    changeStatus: (task: PersonalTask, status: PersonalTaskStatus) => run(() => changePersonalTaskStatus(task.id, status, task.version)),
    remove: (task: PersonalTask) => run(() => deletePersonalTask(task.id, task.version)) };
}
