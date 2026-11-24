import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, createSession, getWorkspaceOptions, resolveApproval, resumeDelegation, resumeRun, switchSessionModel } from "./api";
import { CreateSessionDialog } from "./CreateSessionDialog";
import { Dialog } from "./Dialog";
import { CopyButton, MessageContent } from "./MessageContent";
import { RunActivityPanel } from "./RunActivityPanel";
import { SearchSources } from "./SearchSources";
import { useConversationScroll } from "./useConversationScroll";
import { useSessionWorkspace } from "./useSessionWorkspace";
import { AssistantResults } from "./AssistantResults";
import type { AssistantRunResults, SessionSummary, WorkspaceOptions } from "./types";
import type { AssistantPreviewState } from "./useAssistantResults";

type Workspace = ReturnType<typeof useSessionWorkspace>;
interface ChatWorkspaceProps {
  workspace: Workspace;
  assistantMode?: boolean;
  active?: boolean;
  resetErrorKey?: number;
  ready?: boolean;
  statusMessage?: string;
  draft: string;
  setDraft: (value: string, targetId?: string) => void;
  storageFailed: boolean;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  onOpenNavigation: () => void;
  onOpenSessionMenu?: (session: SessionSummary) => void;
  onFocusTask?: (taskId: string) => void;
  results?: AssistantRunResults[];
  resultErrors?: Array<{ runId: string; message: string }>;
  resultsLoading?: boolean;
  onRetryResults?: (runId: string) => void;
  assistantPreviewFor?: (runId: string, callId: string) => AssistantPreviewState | null;
  onRetryAssistantPreview?: (runId: string) => void;
  targetMessage?: string | null;
  highlight?: string | null;
  onTargetMessageHandled?: () => void;
  onHighlighted?: (id: string) => void;
  onRetryEntry?: () => void;
  showRetryEntry?: boolean;
  externalError?: string;
}

function errorText(error: unknown): string { return error instanceof ApiError || error instanceof Error ? error.message : "请求失败，请稍后再试"; }

/** 普通聊天和助理共用消息、运行控制与输入视图。 */
export function ChatWorkspace({
  workspace, assistantMode = false, ready = true, statusMessage = "", draft, setDraft, storageFailed,
  active = true, resetErrorKey = 0,
  theme, onToggleTheme, onOpenNavigation, onOpenSessionMenu, onFocusTask,
  results = [], resultErrors = [], resultsLoading = false, onRetryResults,
  assistantPreviewFor, onRetryAssistantPreview, targetMessage, highlight, onTargetMessageHandled, onHighlighted, onRetryEntry, showRetryEntry = false,
  externalError = "",
}: ChatWorkspaceProps) {
  const { selected, selectedId, view } = workspace;
  const [options, setOptions] = useState<WorkspaceOptions | null>(null);
  const [nextChoice, setNextChoice] = useState<{ agent_id: string; model: string } | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [modelError, setModelError] = useState("");
  const [modelBusy, setModelBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newError, setNewError] = useState("");
  const submitting = useRef(new Set<string>());
  const composing = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { conversationRef, onScroll, scrollToBottom, awayFromBottom, hasNewContent } = useConversationScroll(selectedId || undefined, view.messages);
  const busy = Boolean(view.abandoningRunId) || workspace.isStreaming(selectedId) || ["running", "stopping", "suspended", "recoverable"].includes(view.phase);
  useEffect(() => { setNewError(""); setModelError(""); }, [selectedId, resetErrorKey]);
  useEffect(() => { if (active && !selectedId) inputRef.current?.focus(); }, [active, selectedId]);
  useEffect(() => {
    if (!targetMessage || view.loading) return;
    const element = document.getElementById(`message-${targetMessage}`);
    if (!element) return;
    element.scrollIntoView({ block: "center" });
    onHighlighted?.(targetMessage);
    onTargetMessageHandled?.();
  }, [targetMessage, view.loading, view.messages, onHighlighted, onTargetMessageHandled]);
  useEffect(() => {
    if (!highlight) return;
    const timer = window.setTimeout(() => onHighlighted?.(""), 2400);
    return () => window.clearTimeout(timer);
  }, [highlight, onHighlighted]);

  async function loadOptions(): Promise<WorkspaceOptions> {
    const value = options || await getWorkspaceOptions();
    if (!value.models.length) throw new Error("没有可用的对话模型，请检查本地模型配置并重启");
    setOptions(value);
    return value;
  }
  async function submit(event?: FormEvent): Promise<void> {
    event?.preventDefault();
    const content = draft.trim();
    const submissionKey = selectedId || "new";
    if (!content || busy || !ready || submitting.current.has(submissionKey)) return;
    submitting.current.add(submissionKey);
    let id = selectedId;
    setNewError("");
    try {
      if (!id) {
        if (assistantMode) throw new Error("助理会话尚未准备好，请重试入口加载");
        setCreating(true);
        const choices = await loadOptions();
        const created = await createSession(nextChoice || { agent_id: choices.default_agent_id, model: choices.default_model });
        id = created.id;
        workspace.upsert(created);
        setDraft(content, id);
        await workspace.open(created);
        setDraft("", "new");
      }
      setDraft("", id);
      scrollToBottom();
      await workspace.send(id, content);
    } catch (error) {
      if (id) workspace.patch(id, { error: errorText(error) });
      else setNewError(errorText(error));
    } finally {
      submitting.current.delete(submissionKey);
      if (submissionKey === "new") setCreating(false);
    }
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); }
  }
  function restoreQuestion(text: string): void {
    if (!text) return;
    setDraft(draft.trim() ? draft + "\n\n" + text : text);
    inputRef.current?.focus();
  }
  async function requestAdvanced(): Promise<void> {
    try { await loadOptions(); setAdvanced(true); } catch (error) { setNewError(errorText(error)); }
  }
  async function requestModel(): Promise<void> {
    setModelError("");
    try { await loadOptions(); setModelOpen(true); } catch (error) { setModelError(errorText(error)); }
  }
  async function changeModel(model: string): Promise<void> {
    if (!selected) {
      if (!assistantMode) setNextChoice({ agent_id: nextChoice?.agent_id || options?.default_agent_id || "default", model });
      setModelOpen(false);
      return;
    }
    setModelBusy(true); setModelError("");
    try { workspace.upsert(await switchSessionModel(selected.id, model)); setModelOpen(false); }
    catch (error) { setModelError(errorText(error)); }
    finally { setModelBusy(false); }
  }
  function controlRun(sessionId: string, messageId: string, runId: string, action: "approve" | "reject" | "delegation" | "resume"): void {
    const current = workspace.views[sessionId];
    const activity = current?.activitiesByRunId[runId];
    if (!activity || current.runId !== runId || current.activeMessageId !== messageId) return;
    if ((action === "approve" || action === "reject") && activity.approval_id) void workspace.control(sessionId, messageId, runId, (callback, signal) => resolveApproval(activity.approval_id!, action === "approve", callback, signal));
    else if (action === "delegation" && activity.child_run_id) void workspace.control(sessionId, messageId, runId, (callback, signal) => resumeDelegation(activity.child_run_id!, callback, signal));
    else if (action === "resume") void workspace.control(sessionId, messageId, runId, (callback, signal) => resumeRun(runId, callback, signal));
  }

  return <>
    <header className="workspace-header">
      <button className="menu-button" type="button" aria-label="打开会话列表" onClick={onOpenNavigation}>☰</button>
      <div className="model-context">
        <button className="model-selector" type="button" disabled={modelBusy || ["running", "stopping"].includes(view.phase) || !ready} onClick={() => void requestModel()}>{selected?.model || nextChoice?.model || "dotClaw"} <span aria-hidden="true">⌄</span></button>
        {modelError && !modelOpen && <span className="inline-error" role="alert">{modelError}</span>}
      </div>
      <span className="header-title" title={selected?.title}>{selected?.title || (assistantMode ? "个人助理" : "新对话")}</span>
      <button className="icon-button theme-button" type="button" aria-label={theme === "light" ? "切换深色外观" : "切换浅色外观"} onClick={onToggleTheme}>{theme === "light" ? "☾" : "☀"}</button>
      {!assistantMode && selected && onOpenSessionMenu && <button className="icon-button" type="button" aria-label="当前会话更多操作" onClick={() => onOpenSessionMenu(selected)}>⋯</button>}
    </header>
    <section className="conversation" aria-label={assistantMode ? "助理对话内容" : "对话内容"} ref={conversationRef} onScroll={onScroll}>
      {statusMessage && <p className="assistant-entry-status" role={ready ? "status" : "alert"}>{statusMessage}{showRetryEntry && onRetryEntry && <button type="button" onClick={onRetryEntry}>重试加载</button>}</p>}
      {workspace.loading || view.loading ? <p className="empty-note muted">正在读取会话…</p> : <div className="message-column">
        {!view.messages.length && !view.activity && <h1 className="welcome-title">{assistantMode ? "告诉我想安排什么" : "有什么可以帮你？"}</h1>}
        {view.messages.map((message) => <article id={`message-${message.id}`} className={`message message--${message.role} ${highlight === message.id ? "message--highlight" : ""}`} key={message.id}>
          <span className="sr-only">{message.role === "user" ? "你" : "dotClaw"}</span>
          <div className="message-body" title={message.created_at}>
            {message.role === "assistant" ? <RunActivityPanel
              message={message}
              activities={(message.run_ids || []).map((id) => view.activitiesByRunId[id]).filter((activity) => Boolean(activity))}
              canControl={view.activeMessageId === message.id && !view.abandoningRunId && !workspace.isStreaming(selectedId) && ["suspended", "recoverable"].includes(view.phase)}
              abandoningRunId={view.abandoningRunId}
              onAbandon={(runId) => selectedId && void workspace.abandon(selectedId, message.id, runId)}
              onLoadRun={(runId) => selectedId && void workspace.loadRunActivity(selectedId, runId)}
              onCheckStatus={(runId) => selectedId && void workspace.checkRunStatus(selectedId, runId)}
              onRestoreInput={() => restoreQuestion(message.execution?.submittedText || "")}
              onApprove={(runId, approved) => selectedId && controlRun(selectedId, message.id, runId, approved ? "approve" : "reject")}
              onResumeDelegation={(runId) => selectedId && controlRun(selectedId, message.id, runId, "delegation")}
              onResumeRun={(runId) => selectedId && controlRun(selectedId, message.id, runId, "resume")}
              assistantPreviewFor={assistantPreviewFor}
              onRetryAssistantPreview={onRetryAssistantPreview}
            >
              <div className="message-content">{!message.content.trim() && (!message.execution || ["completed", "failed", "cancelled"].includes(message.execution.stage)) && !message.pending ? <p className="empty-answer">{message.id.startsWith("local-") ? "本次未获得有效回答" : "该次回答为空"}</p> : <MessageContent content={message.content} />}</div>
            </RunActivityPanel> : <div className="message-content">{message.content}</div>}
          </div>
          {message.role === "assistant" && !message.pending && <div className="message-actions">
            {message.content && <CopyButton getText={() => message.content} label="复制回答" />}
            {message.content && <button type="button" onClick={() => { setDraft(message.content); inputRef.current?.focus(); }}>引用到输入框</button>}
          </div>}
          {message.role === "assistant" && !message.pending && Boolean(message.run_ids?.length) && selectedId && <SearchSources sessionId={selectedId} runIds={message.run_ids!} />}
        </article>)}
        {creating && !selectedId && <article className="message message--assistant"><div className="execution-status" role="status"><span className="execution-spinner" aria-hidden="true" />正在发送</div></article>}
        {assistantMode && <AssistantResults results={results} errors={resultErrors} loading={resultsLoading} onRetry={onRetryResults || (() => undefined)} onOpenTask={onFocusTask || (() => undefined)} />}
      </div>}
    </section>
    <div className="composer-region">
      {(externalError || newError || workspace.loadError || (!view.activeMessageId && view.error)) && <div className="error-strip" role="alert"><p>{externalError || newError || workspace.loadError || view.error}</p></div>}
      <form className="composer" onSubmit={(event) => void submit(event)}>
        <label className="sr-only" htmlFor={assistantMode ? "assistant-message-input" : "message-input"}>{assistantMode ? "输入助理消息" : "输入消息"}</label>
        <textarea ref={inputRef} id={assistantMode ? "assistant-message-input" : "message-input"} value={draft} placeholder={assistantMode ? "描述要处理的事务…" : "输入消息…"} rows={2} disabled={!ready} onChange={(event) => setDraft(event.target.value)} onKeyDown={keyDown} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} />
        {["running", "stopping"].includes(view.phase) && (!view.detached || view.activity?.status === "running") ? <button className="composer-action" type="button" aria-label="停止运行" disabled={view.phase === "stopping"} onClick={() => selectedId && void workspace.stop(selectedId)}>■</button> : <button className="composer-action" aria-label="发送消息" type="submit" disabled={!draft.trim() || busy || !ready || (creating && !selectedId)}>↑</button>}
      </form>
      <div className="composer-hint"><span>Enter 发送 · Shift + Enter 换行</span>{!assistantMode && !selected && <button type="button" onClick={() => void requestAdvanced()}>智能体与模型</button>}{storageFailed && <span role="alert">草稿保存失败</span>}</div>
    </div>
    {awayFromBottom && <button className="back-to-bottom" type="button" onClick={scrollToBottom}>{hasNewContent ? "有新内容 · 回到底部" : "回到底部"} ↓</button>}
    {advanced && options && !assistantMode && <CreateSessionDialog options={options} onCancel={() => setAdvanced(false)} onCreate={(agent_id, model) => { setNextChoice({ agent_id, model }); setAdvanced(false); }} />}
    {modelOpen && options && <Dialog title="选择模型" className="model-dialog" onClose={() => setModelOpen(false)}>
      <p className="muted">用于后续新消息；当前运行保持原模型。智能体：{selected?.agent_id || nextChoice?.agent_id || options.default_agent_id}</p>
      <div className="menu-actions">{options.models.map((model) => <button type="button" aria-pressed={(selected?.model || nextChoice?.model || options.default_model) === model} disabled={modelBusy} key={model} onClick={() => void changeModel(model)}>{model}</button>)}</div>
      {modelError && <p className="inline-error" role="alert">{modelError}</p>}
    </Dialog>}
  </>;
}
