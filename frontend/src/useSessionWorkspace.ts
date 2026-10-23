import { useEffect, useRef, useState } from "react";
import { cancelSession, getRunActivity, getSession, listSessions, submitMessage } from "./api";
import { readPreference, writePreference } from "./preferences";
import type { DisplayMessage, RunActivity, RunPhase, SessionSummary, StreamEvent } from "./types";

export interface ConversationView {
  messages: DisplayMessage[]; phase: RunPhase; activity: RunActivity | null;
  runId: string | null; error: string; loading: boolean; detached: boolean;
}
const emptyView = (): ConversationView => ({ messages: [], phase: "idle", activity: null, runId: null, error: "", loading: false, detached: false });
const activePhase = (phase: RunPhase) => ["running", "stopping", "suspended", "recoverable"].includes(phase);
export function phaseLabel(phase: RunPhase): string {
  return ({ idle: "", running: "正在生成", stopping: "正在停止", suspended: "等待处理", recoverable: "可恢复", completed: "已完成", cancelled: "已停止", failed: "运行异常" })[phase];
}
function phaseFor(activity: RunActivity): RunPhase {
  if (activity.status.startsWith("waiting_")) return "suspended";
  if (activity.status === "abandoned") return "cancelled";
  return (["running", "completed", "failed", "cancelled", "recoverable"].includes(activity.status) ? activity.status : "idle") as RunPhase;
}
function field(data: Record<string, unknown>, key: string): string { return typeof data[key] === "string" ? data[key] as string : ""; }
function errorText(error: unknown): string { return error instanceof Error ? error.message : "请求失败，请稍后再试"; }

/** 以会话为唯一键隔离消息和控制状态，流回调不读取当前选中项。 */
export function useSessionWorkspace() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [views, setViews] = useState<Record<string, ConversationView>>({});
  const viewsRef = useRef(views);
  const streams = useRef(new Map<string, AbortController>());
  const polling = useRef(new Set<string>());
  const terminal = useRef(new Set<string>());
  const revisions = useRef(new Map<string, number>());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const alive = useRef(true);

  function patch(id: string, change: Partial<ConversationView> | ((current: ConversationView) => ConversationView)): void {
    if (!alive.current) return;
    const current = viewsRef.current[id] || emptyView();
    const next = typeof change === "function" ? change(current) : { ...current, ...change };
    viewsRef.current = { ...viewsRef.current, [id]: next };
    setViews(viewsRef.current);
  }
  function upsert(session: SessionSummary): void {
    setSessions((current) => {
      const next = current.some((item) => item.id === session.id) ? current.map((item) => item.id === session.id ? session : item) : [session, ...current];
      return next.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    });
  }
  async function refresh(id: string): Promise<void> {
    const detail = await getSession(id);
    if (!alive.current) return;
    upsert(detail);
    patch(id, (current) => ({ ...current, messages: detail.messages, detached: false, loading: false }));
  }
  async function readActivity(id: string, runId?: string): Promise<void> {
    const revision = revisions.current.get(id) || 0;
    const activity = await getRunActivity(id, runId);
    if (!activity || !alive.current || revision !== (revisions.current.get(id) || 0)) return;
    if (terminal.current.has(activity.run_id) && activePhase(phaseFor(activity))) return;
    const current = viewsRef.current[id];
    if (streams.current.has(id) && activity.status !== "running") return;
    patch(id, { activity, runId: activity.run_id, phase: current?.phase === "stopping" && activity.status === "running" ? "stopping" : phaseFor(activity) });
    if (activity.status === "completed") {
      terminal.current.add(activity.run_id);
      await refresh(id);
    }
  }
  async function open(session: SessionSummary): Promise<void> {
    setSelectedId(session.id);
    writePreference("dotclaw.last-session.v1", session.id);
    if (viewsRef.current[session.id]) return;
    patch(session.id, { loading: true });
    try {
      await refresh(session.id);
      await readActivity(session.id);
      const current = viewsRef.current[session.id];
      if (current?.phase === "running") patch(session.id, { detached: true });
    } catch (error) { patch(session.id, { loading: false, error: errorText(error) }); }
  }
  useEffect(() => {
    alive.current = true;
    void listSessions().then(async (items) => {
      if (!alive.current) return;
      setSessions(items);
      const last = readPreference<string | null>("dotclaw.last-session.v1", null);
      const selected = items.find((item) => item.id === last) || items.find((item) => !item.archived);
      if (selected) await open(selected);
    }).catch((error: unknown) => { if (alive.current) setLoadError(errorText(error)); })
      .finally(() => { if (alive.current) setLoading(false); });
    const timer = window.setInterval(() => {
      for (const [id, view] of Object.entries(viewsRef.current)) {
        if (!activePhase(view.phase) || polling.current.has(id)) continue;
        polling.current.add(id);
        void readActivity(id, view.runId || undefined).catch(() => undefined).finally(() => polling.current.delete(id));
      }
    }, 800);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      for (const controller of streams.current.values()) controller.abort();
      streams.current.clear();
    };
  }, []);

  function startNew(): void {
    setSelectedId(null);
    writePreference("dotclaw.last-session.v1", null);
  }
  function receive(id: string, messageId: string, event: StreamEvent): void {
    const runId = field(event.data, "run_id");
    if (runId) patch(id, { runId });
    if (event.event === "message.delta") {
      const content = field(event.data, "content");
      patch(id, (view) => ({ ...view, messages: view.messages.map((message) => message.id !== messageId ? message : field(event.data, "kind") === "reasoning_delta" ? { ...message, reasoning: (message.reasoning || "") + content } : { ...message, content: message.content + content }) }));
      return;
    }
    const completed = event.event === "run.completed";
    const phase: RunPhase = completed ? "completed" : event.event === "run.cancelled" ? "cancelled" : event.event === "run.suspended" ? "suspended" : "failed";
    if (runId && phase !== "suspended") terminal.current.add(runId);
    const final = event.data.final_message;
    const finalText = final && typeof final === "object" && "content" in final && typeof final.content === "string" ? final.content : "";
    const rawError = event.data.error;
    const message = rawError && typeof rawError === "object" && "message" in rawError ? String(rawError.message) : "";
    patch(id, (view) => ({
      ...view, phase, detached: false,
      activity: view.activity ? { ...view.activity, status: phase === "suspended" ? view.activity.status : phase } : null,
      error: phase === "failed" ? message || "运行失败，请查看执行记录" : "",
      messages: view.messages.map((item) => item.id === messageId ? { ...item, pending: false, content: item.content || finalText, run_ids: runId ? [runId] : item.run_ids } : item),
    }));
  }
  async function runStream(id: string, messageId: string, operation: (onEvent: (event: StreamEvent) => void, signal: AbortSignal) => Promise<void>): Promise<void> {
    if (streams.current.has(id)) return;
    const controller = new AbortController();
    streams.current.set(id, controller);
    patch(id, { phase: "running", error: "", detached: false });
    let receivedTerminal = false;
    let completed = false;
    try {
      await operation((event) => {
        receivedTerminal ||= event.event !== "message.delta";
        completed ||= event.event === "run.completed";
        receive(id, messageId, event);
      }, controller.signal);
      if (!receivedTerminal) patch(id, { phase: "running", detached: true, error: "消息连接已结束，结果尚未确认。请查看运行状态，不要重复发送。" });
      if (completed) await refresh(id);
    } catch (error) {
      if (!controller.signal.aborted) {
        patch(id, { error: errorText(error) + "；结果尚未确认，请查看运行状态，不要自动重发。", detached: true, phase: "running" });
        try {
          const activity = await getRunActivity(id);
          if (!activity) patch(id, { phase: "failed", detached: false });
        } catch { /* 保留未知运行状态，避免误判后重复发送。 */ }
      }
    } finally {
      if (streams.current.get(id) === controller) streams.current.delete(id);
      patch(id, (view) => ({ ...view, messages: view.messages.map((message) => message.id === messageId ? { ...message, pending: false } : message) }));
      try { await readActivity(id, viewsRef.current[id]?.runId || undefined); } catch { /* 现有正文和终态不因记录读取失败而丢失。 */ }
    }
  }
  async function send(id: string, content: string): Promise<void> {
    if (activePhase(viewsRef.current[id]?.phase || "idle") || streams.current.has(id)) return;
    revisions.current.set(id, (revisions.current.get(id) || 0) + 1);
    const createdAt = new Date().toISOString();
    const messageId = `local-assistant-${crypto.randomUUID()}`;
    patch(id, (view) => ({ ...view, activity: null, runId: null, messages: [
      ...view.messages,
      { id: `local-user-${crypto.randomUUID()}`, role: "user", content, created_at: createdAt },
      { id: messageId, role: "assistant", content: "", reasoning: "", created_at: createdAt, pending: true },
    ] }));
    await runStream(id, messageId, (callback, signal) => submitMessage(id, content, callback, signal));
  }
  async function control(id: string, operation: (callback: (event: StreamEvent) => void, signal: AbortSignal) => Promise<void>): Promise<void> {
    if (streams.current.has(id)) return;
    revisions.current.set(id, (revisions.current.get(id) || 0) + 1);
    const messageId = `local-control-${crypto.randomUUID()}`;
    patch(id, (view) => ({ ...view, messages: [...view.messages, { id: messageId, role: "assistant", content: "", created_at: new Date().toISOString(), pending: true }] }));
    await runStream(id, messageId, operation);
  }
  async function stop(id: string): Promise<void> {
    patch(id, { phase: "stopping", error: "" });
    try { await cancelSession(id); } catch (error) { patch(id, { phase: "running", error: errorText(error) }); }
  }
  function remove(id: string): void {
    setSessions((current) => current.filter((session) => session.id !== id));
    const next = { ...viewsRef.current }; delete next[id]; viewsRef.current = next; setViews(next);
    if (selectedId === id) startNew();
  }
  return { sessions, selectedId, selected: sessions.find((session) => session.id === selectedId) || null,
    views, view: selectedId ? views[selectedId] || emptyView() : emptyView(), loading, loadError,
    isStreaming: (id: string | null) => Boolean(id && streams.current.has(id)),
    open, startNew, upsert, refresh, send, control, stop, patch, remove };
}
