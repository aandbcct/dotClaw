import { useEffect, useRef, useState } from "react";
import { ApiError, cancelSession, getRunActivity, getSession, listSessions, submitMessage } from "./api";
import { readPreference, writePreference } from "./preferences";
import type { DisplayMessage, MessageExecution, RunAcceptedData, RunActivity, RunPhase, SessionSummary, StreamEvent } from "./types";

export interface ConversationView {
  messages: DisplayMessage[]; phase: RunPhase; activity: RunActivity | null;
  runId: string | null; error: string; loading: boolean; detached: boolean;
  errorCode: string; retryable: boolean; lastSubmittedText: string; statusError: string;
  submissionStage: "idle" | "sending" | "accepted" | "streaming";
  activeMessageId: string | null;
  activitiesByRunId: Record<string, RunActivity>;
}
const emptyView = (): ConversationView => ({ messages: [], phase: "idle", activity: null, runId: null, error: "", loading: false, detached: false, errorCode: "", retryable: false, lastSubmittedText: "", statusError: "", submissionStage: "idle", activeMessageId: null, activitiesByRunId: {} });
const newExecution = (submittedText = ""): MessageExecution => ({ stage: "processing", error: null, statusError: "", submittedText });
const finalStages = new Set(["completed", "failed", "cancelled"]);

/** 控制字段与当前消息在同一次状态替换中更新，旧消息不读取当前运行状态。 */
function patchMessageExecution(view: ConversationView): ConversationView {
  if (!view.activeMessageId) return view;
  return { ...view, messages: view.messages.map((message) => {
    if (message.id !== view.activeMessageId) return message;
    const execution = message.execution || newExecution(view.lastSubmittedText);
    let stage = execution.stage;
    if (view.detached) stage = "unknown";
    else if (view.phase === "stopping") stage = "stopping";
    else if (view.phase === "suspended") stage = view.activity?.status === "waiting_delegation" ? "waiting_delegation" : "waiting_approval";
    else if (view.phase === "recoverable") stage = "recoverable";
    else if (["completed", "failed", "cancelled"].includes(view.phase)) stage = view.phase as MessageExecution["stage"];
    else if (view.submissionStage === "sending") stage = "sending";
    else if (view.submissionStage === "accepted" && !["thinking", "responding", "tool_running"].includes(stage)) stage = "processing";
    return { ...message, run_ids: view.runId ? Array.from(new Set([...(message.run_ids || []), view.runId])) : message.run_ids,
      execution: { ...execution, stage, error: view.error ? { code: view.errorCode, message: view.error, retryable: view.retryable } : null, statusError: view.statusError } };
  }) };
}
const terminalEvents = new Set(["run.completed", "run.failed", "run.cancelled", "run.suspended", "stream.error"]);
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
function isRunAcceptedData(data: Record<string, unknown>): data is Record<string, unknown> & RunAcceptedData {
  return typeof data.session_id === "string" && Boolean(data.session_id.trim()) && typeof data.run_id === "string" && Boolean(data.run_id.trim());
}

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
  const deltaVersions = useRef(new Map<string, number>());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const alive = useRef(true);

  function patch(id: string, change: Partial<ConversationView> | ((current: ConversationView) => ConversationView), syncExecution = true): void {
    if (!alive.current) return;
    const current = viewsRef.current[id] || emptyView();
    const changed = typeof change === "function" ? change(current) : { ...current, ...change };
    const next = syncExecution ? patchMessageExecution(changed) : changed;
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
    patch(id, (current) => {
      const mergedRequests = new Set<string>();
      const mergedIds = new Map<string, string>();
      const messages: DisplayMessage[] = detail.messages.map((saved) => {
        if (saved.role !== "assistant") return saved;
        const local = current.messages.find((item) => item.role === "assistant" && (item.id === saved.id || item.run_ids?.some((runId) => saved.run_ids?.includes(runId))));
        if (!local) return saved;
        mergedIds.set(local.id, saved.id);
        if (local.requestId) mergedRequests.add(local.requestId);
        return { ...saved, reasoning: local.reasoning, execution: local.execution, requestId: local.requestId, pending: local.pending };
      });
      // 只删除已按运行标识确认入库的本地配对，不按相同文本猜测消息身份。
      for (let index = 0; index < current.messages.length; index++) {
        const item = current.messages[index];
        if (!item.id.startsWith("local-") || mergedIds.has(item.id) || (item.requestId && mergedRequests.has(item.requestId))) continue;
        const following = current.messages.slice(index + 1).find((candidate) => mergedIds.has(candidate.id));
        const anchor = following && messages.findIndex((candidate) => candidate.id === mergedIds.get(following.id));
        // 未入库的失败请求仍留在随后已入库请求之前，不能因刷新被搬到末尾。
        const insertAt = typeof anchor === "number" && anchor >= 0 ? (messages[anchor - 1]?.role === "user" ? anchor - 1 : anchor) : messages.length;
        messages.splice(insertAt, 0, item);
      }
      return { ...current, messages, activeMessageId: mergedIds.get(current.activeMessageId || "") || current.activeMessageId, loading: false };
    });
  }
  async function readActivity(id: string, runId?: string, historyOnly = false): Promise<void> {
    const revision = revisions.current.get(id) || 0;
    const knownRunId = runId || viewsRef.current[id]?.runId || undefined;
    const targetId = viewsRef.current[id]?.messages.find((message) => message.run_ids?.includes(knownRunId || ""))?.id || viewsRef.current[id]?.activeMessageId;
    const version = deltaVersions.current.get(`${id}:${targetId}`) || 0;
    const statusError = (text: string) => {
      if (!historyOnly) patch(id, { statusError: text });
      else patch(id, (view) => ({ ...view, messages: view.messages.map((message) => message.run_ids?.includes(knownRunId || "") ? { ...message, execution: { ...(message.execution || { ...newExecution(), stage: "completed" }), statusError: text } } : message) }), false);
    };
    let activity: RunActivity | null;
    try { activity = await getRunActivity(id, knownRunId); }
    catch (error) {
      if (historyOnly || revision === (revisions.current.get(id) || 0)) statusError("状态查询失败：" + errorText(error));
      return;
    }
    if (!alive.current || (!historyOnly && revision !== (revisions.current.get(id) || 0))) return;
    const expectedRunId = knownRunId || viewsRef.current[id]?.runId;
    if (activity && expectedRunId && activity.run_id !== expectedRunId) {
      statusError("状态查询失败：返回的运行与本次请求不一致"); return;
    }
    statusError("");
    // 无活动运行不等于本次请求未执行，尤其在接收运行 ID 之前断线时。
    if (!activity) { if (historyOnly) statusError("该运行记录不存在或不属于此会话"); return; }
    const current = viewsRef.current[id] || emptyView();
    const cached = current.activitiesByRunId[activity.run_id];
    if ((activity.event_sequence || 0) < (cached?.event_sequence || 0)) return;
    const settled = terminal.current.has(activity.run_id) || (cached && ["completed", "failed", "cancelled", "abandoned"].includes(cached.status) && !cached.error?.retryable);
    if (settled && activePhase(phaseFor(activity))) {
      const activityRunId = activity.run_id;
      const messageStage = current.messages.find((message) => message.run_ids?.includes(activityRunId))?.execution?.stage;
      const confirmedStatus = cached?.status && !activePhase(phaseFor(cached)) ? cached.status : messageStage && finalStages.has(messageStage) ? messageStage : current.phase;
      // 迟到的运行中快照不能撤销 SSE 终态；没有完成证据的开放步骤只标记中断。
      activity = { ...activity, status: confirmedStatus, steps: activity.steps?.map((step) => step.status !== "running" ? step : { ...step, status: confirmedStatus === "failed" ? "failed" : confirmedStatus === "cancelled" ? "cancelled" : "interrupted" }) };
    }
    if (activity.status === "running" && current.runId === activity.run_id && ["suspended", "recoverable"].includes(current.phase)) {
      // 只有显式控制入口会解除等待态，迟到的普通轮询不能撤销审批或恢复入口。
      activity = { ...activity, status: current.activity?.status || cached?.status || "recoverable", approval_id: current.activity?.approval_id || null, child_run_id: current.activity?.child_run_id || null };
    }
    const acceptedActivity = activity;
    patch(id, (view) => ({ ...view, activitiesByRunId: { ...view.activitiesByRunId, [acceptedActivity.run_id]: acceptedActivity } }), false);
    if (historyOnly) return;
    if (!knownRunId && ["completed", "failed", "cancelled", "abandoned"].includes(activity.status)) return;
    // 新请求尚未取得运行标识时，最近活动运行可能属于其他请求，不能冒充确认。
    if (streams.current.has(id) && current?.submissionStage === "sending" && !current.runId) return;
    if (streams.current.has(id) && activity.status !== "running") return;
    const resolvedError = activity.error ? { error: activity.error.message, errorCode: activity.error.code, retryable: activity.error.retryable === true }
      : activity.status === "failed" ? { error: current?.error && !current.detached ? current.error : "运行失败，请查看执行记录", errorCode: current?.errorCode && !current.detached ? current.errorCode : "run_failed", retryable: current?.retryable || false }
      : current?.detached && activity.status !== "running" ? { error: "", errorCode: "", retryable: false } : {};
    let message = current.messages.find((item) => item.run_ids?.includes(activity.run_id));
    if (!message) message = current.messages.find((item) => item.id === current.activeMessageId && !item.run_ids?.length);
    const messageId = message?.id || `local-run-${activity.run_id}`;
    const model = activity.steps?.filter((step) => step.kind === "model").at(-1);
    const previousModel = cached?.steps?.filter((step) => step.kind === "model").at(-1);
    const noNewText = version === (deltaVersions.current.get(`${id}:${messageId}`) || 0);
    let stage = message?.execution?.stage || "processing";
    if (noNewText && activity.status === "running" && !finalStages.has(stage) && !["unknown", "stopping"].includes(stage)) {
      if (activity.steps?.some((step) => step.kind === "tool" && step.status === "running") || (!activity.steps && activity.tools.some((tool) => tool.status === "running"))) stage = "tool_running";
      else if (model?.status === "running" && previousModel && model.id !== previousModel.id) stage = "processing";
    }
    patch(id, { activity, runId: activity.run_id, activeMessageId: messageId,
      messages: message ? current.messages.map((item) => item.id === messageId ? { ...item, execution: { ...(item.execution || newExecution()), stage } } : item) : [...current.messages, { id: messageId, role: "assistant", content: "", created_at: "", run_ids: [activity.run_id], execution: newExecution() }],
      phase: current?.phase === "stopping" && activity.status === "running" ? "stopping" : phaseFor(activity), detached: activity.status === "running" && (!streams.current.has(id) || current?.errorCode === "invalid_run_accepted"),
      ...resolvedError,
      ...(activity.status !== "running" ? { submissionStage: "idle" as const } : {}),
    });
    if (["completed", "failed", "cancelled", "abandoned"].includes(activity.status) && !activity.error?.retryable) terminal.current.add(activity.run_id);
    if (activity.status === "completed") {
      terminal.current.add(activity.run_id);
      try { await refresh(id); } catch (error) { patch(id, { statusError: "状态查询失败：完成记录读取失败，" + errorText(error) }); }
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
        if ((!activePhase(view.phase) && !view.detached) || polling.current.has(id)) continue;
        polling.current.add(id);
        void readActivity(id, view.runId || undefined).finally(() => polling.current.delete(id));
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
    const current = viewsRef.current[id] || emptyView();
    const protocolError = () => patch(id, { error: "运行接收确认协议异常；结果待确认，请查询运行状态，不要重复发送。", errorCode: "invalid_run_accepted", retryable: false, detached: true, submissionStage: "idle" });
    if (event.event === "run.accepted") {
      if (!isRunAcceptedData(event.data) || event.data.session_id !== id || (current.runId && current.runId !== event.data.run_id)) {
        protocolError(); return;
      }
      if (terminal.current.has(event.data.run_id)) return;
      patch(id, { runId: event.data.run_id, submissionStage: current.submissionStage === "streaming" ? "streaming" : current.detached ? "idle" : "accepted" });
      return;
    }
    if (event.event !== "message.delta" && !terminalEvents.has(event.event)) return;
    const runId = field(event.data, "run_id");
    if ((typeof event.data.session_id === "string" && event.data.session_id !== id) || (runId && current.runId && current.runId !== runId)) {
      protocolError(); return;
    }
    if (runId) patch(id, { runId });
    if (runId && terminal.current.has(runId)) return;
    if (event.event === "message.delta") {
      const content = field(event.data, "content");
      if (content) deltaVersions.current.set(`${id}:${messageId}`, (deltaVersions.current.get(`${id}:${messageId}`) || 0) + 1);
      patch(id, (view) => ({ ...view, submissionStage: content && !view.detached ? "streaming" : view.submissionStage, messages: view.messages.map((message) => message.id !== messageId ? message : {
        ...message, execution: { ...(message.execution || newExecution()), stage: content ? field(event.data, "kind") === "reasoning_delta" ? "thinking" : "responding" : message.execution?.stage || "processing" },
        ...(field(event.data, "kind") === "reasoning_delta" ? { reasoning: (message.reasoning || "") + content } : { content: message.content + content }),
      }) }));
      return;
    }
    const completed = event.event === "run.completed";
    const phase: RunPhase = completed ? "completed" : event.event === "run.cancelled" ? "cancelled" : event.event === "run.suspended" ? "suspended" : "failed";
    const final = event.data.final_message;
    const finalText = final && typeof final === "object" && "content" in final && typeof final.content === "string" ? final.content : "";
    const rawError = event.data.error;
    const message = rawError && typeof rawError === "object" && "message" in rawError ? String(rawError.message) : "";
    const code = rawError && typeof rawError === "object" && "code" in rawError ? String(rawError.code) : "";
    const retryable = Boolean(rawError && typeof rawError === "object" && "retryable" in rawError && rawError.retryable === true);
    const unknown = event.event === "stream.error";
    if (runId && phase !== "suspended" && !unknown && !retryable) terminal.current.add(runId);
    patch(id, (view) => {
      const prior = runId ? view.activitiesByRunId[runId] : null;
      const activity: RunActivity | null = runId && !unknown ? { ...prior, run_id: runId, tools: prior?.tools || [], status: phase === "suspended" ? field(event.data, "status") || "waiting_approval" : phase,
        approval_id: field(event.data, "approval_id") || null, child_run_id: field(event.data, "child_run_id") || null,
        error: phase === "failed" ? { code, message: message || "运行失败，请查看执行记录", retryable } : null } : view.activity;
      return {
      ...view, phase: unknown ? "running" : phase, detached: unknown, submissionStage: "idle", activity,
      activitiesByRunId: activity ? { ...view.activitiesByRunId, [activity.run_id]: activity } : view.activitiesByRunId,
      error: unknown ? (message || "消息连接异常") + "；结果待确认，请查询运行状态，不要重复发送。" : phase === "failed" ? message || "运行失败，请查看执行记录" : "",
      errorCode: code, retryable, statusError: "",
      messages: view.messages.map((item) => item.id === messageId ? { ...item, pending: false, content: item.content || finalText, run_ids: runId ? Array.from(new Set([...(item.run_ids || []), runId])) : item.run_ids } : item),
    }; });
  }
  async function runStream(id: string, messageId: string, operation: (onEvent: (event: StreamEvent) => void, signal: AbortSignal) => Promise<void>): Promise<void> {
    if (streams.current.has(id)) return;
    const controller = new AbortController();
    streams.current.set(id, controller);
    patch(id, { phase: "running", error: "", errorCode: "", retryable: false, statusError: "", detached: false });
    let receivedTerminal = false;
    let completed = false;
    let queryStatus = false;
    try {
      await operation((event) => {
        const knownRunId = viewsRef.current[id]?.runId;
        const eventRunId = field(event.data, "run_id");
        const matchesRequest = (!field(event.data, "session_id") || event.data.session_id === id) && (!knownRunId || !eventRunId || knownRunId === eventRunId);
        // 串入其他运行的结束事件不能触发历史刷新或把当前请求判为已结束。
        receivedTerminal ||= matchesRequest && terminalEvents.has(event.event);
        completed ||= matchesRequest && event.event === "run.completed";
        receive(id, messageId, event);
      }, controller.signal);
      if (!receivedTerminal && !viewsRef.current[id]?.detached) patch(id, { phase: "running", detached: true, submissionStage: "idle", errorCode: "result_unknown", error: "消息连接已结束，结果待确认。请查询运行状态，不要重复发送。" });
      queryStatus = !receivedTerminal || completed || viewsRef.current[id]?.detached || viewsRef.current[id]?.retryable || viewsRef.current[id]?.phase === "suspended";
      if (completed) {
        try { await refresh(id); }
        catch (error) { patch(id, { statusError: "状态查询失败：完成记录读取失败，" + errorText(error) }); }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        if (receivedTerminal && !viewsRef.current[id]?.detached) {
          // 已确认业务终态不能被随后连接或查询错误改写。
          patch(id, { statusError: "消息连接结束异常，已保留确认的运行结果" });
        } else if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          patch(id, { error: error.message, errorCode: error.code, retryable: error.retryable, detached: false, phase: "failed", submissionStage: "idle" });
          queryStatus = error.status === 409 || error.retryable;
        } else {
          patch(id, { error: (error instanceof ApiError ? error.message : "消息连接中断") + "；结果待确认，请查询运行状态，不要重复发送。", errorCode: error instanceof ApiError ? error.code : "result_unknown", retryable: error instanceof ApiError ? error.retryable : false, detached: true, phase: "running", submissionStage: "idle" });
          queryStatus = true;
        }
      }
    } finally {
      if (streams.current.get(id) === controller) streams.current.delete(id);
      patch(id, (view) => ({ ...view, messages: view.messages.map((message) => message.id === messageId ? { ...message, pending: false } : message) }));
      if (queryStatus) await readActivity(id, viewsRef.current[id]?.runId || undefined);
    }
  }
  async function send(id: string, content: string): Promise<void> {
    if (activePhase(viewsRef.current[id]?.phase || "idle") || streams.current.has(id)) return;
    revisions.current.set(id, (revisions.current.get(id) || 0) + 1);
    const createdAt = new Date().toISOString();
    const messageId = `local-assistant-${crypto.randomUUID()}`;
    const requestId = crypto.randomUUID();
    patch(id, (view) => ({ ...view, activity: null, runId: null, activeMessageId: messageId, submissionStage: "sending", lastSubmittedText: content, error: "", errorCode: "", retryable: false, statusError: "", detached: false, phase: "running", messages: [
      ...view.messages,
      { id: `local-user-${crypto.randomUUID()}`, requestId, role: "user", content, created_at: createdAt },
      { id: messageId, requestId, role: "assistant", content: "", reasoning: "", created_at: createdAt, pending: true, execution: { ...newExecution(content), stage: "sending" } },
    ] }));
    await runStream(id, messageId, (callback, signal) => submitMessage(id, content, callback, signal));
  }
  async function control(id: string, targetMessageId: string, runId: string, operation: (callback: (event: StreamEvent) => void, signal: AbortSignal) => Promise<void>): Promise<void> {
    if (streams.current.has(id) || viewsRef.current[id]?.runId !== runId || !["suspended", "recoverable"].includes(viewsRef.current[id]?.phase)) return;
    revisions.current.set(id, (revisions.current.get(id) || 0) + 1);
    const messages = viewsRef.current[id]?.messages || [];
    const messageId = (messages.find((message) => message.id === targetMessageId && message.run_ids?.includes(runId)) || messages.find((message) => message.role === "assistant" && message.run_ids?.includes(runId)))?.id || `local-run-${runId}`;
    patch(id, (view) => ({ ...view, activeMessageId: messageId, submissionStage: "idle", phase: "running", detached: false, error: "", statusError: "", messages: view.messages.some((message) => message.id === messageId) ? view.messages.map((message) => message.id === messageId ? { ...message, pending: true, execution: { ...(message.execution || newExecution()), stage: "processing" } } : message) : [...view.messages, { id: messageId, role: "assistant", content: "", run_ids: [runId], created_at: "", pending: true, execution: newExecution() }] }));
    await runStream(id, messageId, operation);
  }
  async function stop(id: string): Promise<void> {
    const previousPhase = viewsRef.current[id]?.phase || "running";
    const messageId = viewsRef.current[id]?.activeMessageId;
    const previousStage = viewsRef.current[id]?.messages.find((message) => message.id === messageId)?.execution?.stage;
    patch(id, { phase: "stopping" });
    try { await cancelSession(id); } catch (error) {
      const text = "停止请求失败：" + errorText(error);
      patch(id, (view) => ({ ...view,
        ...(view.activeMessageId === messageId ? { statusError: text, phase: view.phase === "stopping" ? previousPhase : view.phase } : {}),
        messages: view.messages.map((message) => message.id === messageId && message.execution ? { ...message, execution: { ...message.execution, statusError: text, stage: view.phase === "stopping" && view.activeMessageId === messageId ? previousStage || "processing" : message.execution.stage } } : message),
      }));
    }
  }
  async function checkRunStatus(id: string, runId?: string): Promise<void> {
    await readActivity(id, runId || viewsRef.current[id]?.runId || undefined, Boolean(runId && runId !== viewsRef.current[id]?.runId));
  }
  async function loadRunActivity(id: string, runId: string): Promise<void> {
    await readActivity(id, runId, true);
  }
  function remove(id: string): void {
    setSessions((current) => current.filter((session) => session.id !== id));
    const next = { ...viewsRef.current }; delete next[id]; viewsRef.current = next; setViews(next);
    if (selectedId === id) startNew();
  }
  return { sessions, selectedId, selected: sessions.find((session) => session.id === selectedId) || null,
    views, view: selectedId ? views[selectedId] || emptyView() : emptyView(), loading, loadError,
    isStreaming: (id: string | null) => Boolean(id && streams.current.has(id)),
    open, startNew, upsert, refresh, send, control, stop, checkRunStatus, loadRunActivity, patch, remove };
}
