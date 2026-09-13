import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { CopyButton, MessageContent } from "./MessageContent";
import { CreateSessionDialog } from "./CreateSessionDialog";
import { RunActivityPanel } from "./RunActivityPanel";
import { SessionList } from "./SessionList";
import { useDraft } from "./useDraft";
import { useConversationScroll } from "./useConversationScroll";

import {
  ApiError,
  cancelSession,
  createSession,
  getRunActivity,
  getSession,
  getWorkspaceOptions,
  listSessions,
  resolveApproval,
  resumeDelegation,
  resumeRun,
  submitMessage,
  updateSession,
} from "./api";
import type {
  DisplayMessage,
  RunActivity,
  RunPhase,
  SessionDetail,
  SessionSummary,
  StreamEvent,
  WorkspaceOptions,
} from "./types";

const TERMINAL_EVENTS = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.suspended",
  "stream.error",
]);

export function App() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      return localStorage.getItem("dotclaw.theme") === "dark" ? "dark" : "light";
    } catch {
      return "light";
    }
  });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSession, setSelectedSession] = useState<SessionSummary | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const { draft, setDraft, storageFailed } = useDraft(selectedSession?.id);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [createOptions, setCreateOptions] = useState<WorkspaceOptions | null>(null);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [runPhase, setRunPhase] = useState<RunPhase>("idle");
  const [runActivity, setRunActivity] = useState<RunActivity | null>(null);
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const detailRequestId = useRef(0);
  const streamController = useRef<AbortController | null>(null);
  const controlInFlight = useRef(false);
  const terminalRunIds = useRef(new Set<string>());
  const composing = useRef(false);
  const { conversationRef, onScroll, scrollToBottom, awayFromBottom, hasNewContent } = useConversationScroll(selectedSession?.id, messages);

  const isRunning = runPhase === "running" || runPhase === "stopping";

  useEffect(() => {
    try {
      localStorage.setItem("dotclaw.theme", theme);
    } catch {
      // 浏览器禁用本地存储时，仍允许切换当前页面的外观。
    }
  }, [theme]);

  useEffect(() => {
    void loadWorkspace();
    return () => streamController.current?.abort();
  }, []);

  useEffect(() => {
    if (selectedSession === null || !["running", "stopping", "suspended", "recoverable"].includes(runPhase)) {
      return;
    }
    let active = true;
    const refresh = async () => {
      try {
        const activity = await getRunActivity(selectedSession.id, currentRunId ?? undefined);
        if (active && activity !== null) {
          if ((controlInFlight.current || terminalRunIds.current.has(activity.run_id)) && ["waiting_approval", "waiting_delegation", "recoverable"].includes(activity.status)) {
            return;
          }
          setRunActivity(activity);
          setCurrentRunId(activity.run_id);
          syncActivityPhase(activity);
          if (activity.status === "completed") {
            await refreshCompletedSession(selectedSession.id);
          }
        }
      } catch {
        // 短暂读取失败不覆盖正在进行的回答与控制状态。
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 600);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [selectedSession?.id, currentRunId, runPhase]);

  async function loadWorkspace(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const availableSessions = await listSessions();
      setSessions(availableSessions);
      const latest = availableSessions.find((session) => !session.archived);
      if (latest) {
        await openSession(latest);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }

  async function openSession(session: SessionSummary): Promise<void> {
    if (isRunning || updating) {
      return;
    }
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setSelectedSession(session);
    setMessages([]);
    setError(null);
    setRunPhase("idle");
    setRunActivity(null);
    setCurrentRunId(null);
    terminalRunIds.current.clear();
    setSidebarOpen(false);
    try {
      const detail = await getSession(session.id);
      if (detailRequestId.current === requestId) {
        setSelectedSession(detail);
        setMessages(detail.messages);
        try {
          const activity = await getRunActivity(session.id);
          if (detailRequestId.current === requestId && activity !== null) {
            setRunActivity(activity);
            setCurrentRunId(activity.run_id);
            syncActivityPhase(activity);
          }
        } catch {
          // 会话历史仍可使用，运行状态读取失败不阻塞打开会话。
        }
      }
    } catch (caught) {
      if (detailRequestId.current === requestId) {
        setError(errorMessage(caught));
      }
    }
  }

  async function handleRequestCreateSession(): Promise<void> {
    if (creating || isRunning || updating) return;
    setError(null);
    try {
      const options = createOptions ?? await getWorkspaceOptions();
      setCreateOptions(options);
      setShowCreateDialog(true);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  async function handleCreateSession(agentId: string, model: string): Promise<void> {
    if (creating || isRunning || updating) {
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const created = await createSession({ agent_id: agentId, model });
      setSessions((current) => [created, ...current]);
      setShowCreateDialog(false);
      await openSession(created);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setCreating(false);
    }
  }

  async function handleSubmit(event?: FormEvent): Promise<void> {
    event?.preventDefault();
    const content = draft.trim();
    if (selectedSession === null || !content || isRunning || updating) {
      return;
    }

    const sessionId = selectedSession.id;
    const createdAt = new Date().toISOString();
    const userMessageId = `local-user-${crypto.randomUUID()}`;
    const assistantMessageId = `local-assistant-${crypto.randomUUID()}`;
    setMessages((current) => [
      ...current,
      { id: userMessageId, role: "user", content, created_at: createdAt },
      {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        reasoning: "",
        created_at: createdAt,
        pending: true,
      },
    ]);
    setDraft("");
    scrollToBottom();
    setError(null);
    setRunPhase("running");
    setRunActivity(null);
    setCurrentRunId(null);

    const controller = new AbortController();
    streamController.current = controller;
    let terminalReceived = false;
    let completed = false;

    try {
      await submitMessage(
        sessionId,
        content,
        (streamEvent) => {
          if (TERMINAL_EVENTS.has(streamEvent.event)) {
            terminalReceived = true;
          }
          if (streamEvent.event === "run.completed") {
            completed = true;
          }
          const runId = stringField(streamEvent.data, "run_id");
          if (runId) setCurrentRunId(runId);
          applyStreamEvent(streamEvent, assistantMessageId);
        },
        controller.signal,
      );
      if (!terminalReceived) {
        setRunPhase("failed");
        setError("消息流提前结束，请刷新后确认运行结果");
      } else if (completed) {
        await refreshCompletedSession(sessionId);
      }
    } catch (caught) {
      if (!controller.signal.aborted) {
        setRunPhase("failed");
        setError(errorMessage(caught));
        markAssistantFinished(assistantMessageId);
      }
    } finally {
      if (streamController.current === controller) {
        streamController.current = null;
      }
    }
  }

  function applyStreamEvent(
    streamEvent: StreamEvent,
    assistantMessageId: string,
  ): void {
    if (streamEvent.event === "message.delta") {
      const content = stringField(streamEvent.data, "content");
      const kind = stringField(streamEvent.data, "kind");
      setMessages((current) =>
        current.map((message) => {
          if (message.id !== assistantMessageId) {
            return message;
          }
          if (kind === "reasoning_delta") {
            return { ...message, reasoning: `${message.reasoning ?? ""}${content}` };
          }
          return { ...message, content: `${message.content}${content}` };
        }),
      );
      return;
    }

    markAssistantFinished(assistantMessageId);
    if (streamEvent.event === "run.completed") {
      terminalRunIds.current.add(stringField(streamEvent.data, "run_id"));
      const finalContent = nestedStringField(streamEvent.data, "final_message", "content");
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantMessageId && !message.content
            ? { ...message, content: finalContent }
            : message,
        ),
      );
      setRunPhase("completed");
      setRunActivity((current) => current ? { ...current, status: "completed" } : current);
      return;
    }
    if (streamEvent.event === "run.cancelled") {
      terminalRunIds.current.add(stringField(streamEvent.data, "run_id"));
      setRunPhase("cancelled");
      setRunActivity((current) => current ? { ...current, status: "cancelled" } : current);
      return;
    }
    if (streamEvent.event === "run.suspended") {
      setRunPhase("suspended");
      return;
    }

    setRunPhase("failed");
    terminalRunIds.current.add(stringField(streamEvent.data, "run_id"));
    setRunActivity((current) => current ? { ...current, status: "failed" } : current);
    setError(
      nestedStringField(streamEvent.data, "error", "message") ||
        "运行失败，请稍后重试",
    );
  }

  function markAssistantFinished(assistantMessageId: string): void {
    setMessages((current) =>
      current.map((message) =>
        message.id === assistantMessageId
          ? { ...message, pending: false }
          : message,
      ),
    );
  }

  async function refreshCompletedSession(sessionId: string): Promise<void> {
    try {
      const [detail, availableSessions] = await Promise.all([
        getSession(sessionId),
        listSessions(),
      ]);
      setSelectedSession(detail);
      setMessages(detail.messages);
      setSessions(availableSessions);
      if (currentRunId) {
        try {
          setRunActivity(await getRunActivity(sessionId, currentRunId));
        } catch {
          // 对话投影已经刷新，执行记录读取失败不改变成功状态。
        }
      }
    } catch {
      // 流式回答已经完整展示，刷新投影失败不覆盖本次成功结果。
    }
  }

  function syncActivityPhase(activity: RunActivity): void {
    if (activity.status === "waiting_approval" || activity.status === "waiting_delegation") {
      if (runPhase !== "running") setRunPhase("suspended");
    } else if (activity.status === "recoverable") {
      setRunPhase("recoverable");
    } else if (activity.status === "running") {
      setRunPhase("running");
    } else if (activity.status === "completed") {
      setRunPhase("completed");
    } else if (activity.status === "cancelled" || activity.status === "abandoned") {
      setRunPhase("cancelled");
    } else if (activity.status === "failed") {
      setRunPhase("failed");
    }
  }

  async function handleRunControl(
    operation: (
      onEvent: (event: StreamEvent) => void,
      signal: AbortSignal,
    ) => Promise<void>,
  ): Promise<void> {
    if (selectedSession === null || isRunning) return;
    setError(null);
    setRunPhase("running");
    controlInFlight.current = true;
    let assistantMessageId = [...messages].reverse().find((item) => item.role === "assistant")?.id;
    if (!assistantMessageId) {
      assistantMessageId = `local-assistant-${crypto.randomUUID()}`;
      setMessages((current) => [...current, {
        id: assistantMessageId as string,
        role: "assistant",
        content: "",
        reasoning: "",
        created_at: new Date().toISOString(),
        pending: true,
      }]);
    } else {
      const targetId = assistantMessageId;
      setMessages((current) => current.map((item) => item.id === targetId ? { ...item, pending: true } : item));
    }
    const controller = new AbortController();
    streamController.current = controller;
    let terminalReceived = false;
    let completed = false;
    try {
      await operation((streamEvent) => {
        terminalReceived ||= TERMINAL_EVENTS.has(streamEvent.event);
        completed ||= streamEvent.event === "run.completed";
        const runId = stringField(streamEvent.data, "run_id");
        if (runId) setCurrentRunId(runId);
        applyStreamEvent(streamEvent, assistantMessageId as string);
      }, controller.signal);
      if (!terminalReceived) {
        setRunPhase("failed");
        setError("消息流提前结束，请刷新后确认运行结果");
      } else if (completed) {
        await refreshCompletedSession(selectedSession.id);
      }
    } catch (caught) {
      if (!controller.signal.aborted) {
        setRunPhase("failed");
        setError(errorMessage(caught));
        markAssistantFinished(assistantMessageId);
      }
    } finally {
      controlInFlight.current = false;
      if (streamController.current === controller) streamController.current = null;
    }
  }

  function handleApproval(approved: boolean): void {
    const approvalId = runActivity?.approval_id;
    if (!approvalId) return;
    void handleRunControl((onEvent, signal) => resolveApproval(approvalId, approved, onEvent, signal));
  }

  function handleResumeDelegation(): void {
    const childRunId = runActivity?.child_run_id;
    if (!childRunId) return;
    void handleRunControl((onEvent, signal) => resumeDelegation(childRunId, onEvent, signal));
  }

  function handleResumeRun(): void {
    const runId = runActivity?.run_id;
    if (!runId) return;
    void handleRunControl((onEvent, signal) => resumeRun(runId, onEvent, signal));
  }

  async function handleStop(): Promise<void> {
    if (selectedSession === null || runPhase !== "running") {
      return;
    }
    setRunPhase("stopping");
    setError(null);
    try {
      await cancelSession(selectedSession.id);
    } catch (caught) {
      setRunPhase("running");
      setError(errorMessage(caught));
    }
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
    }
  }

  async function handleUpdateSession(
    session: SessionSummary, changes: { title?: string; archived?: boolean },
  ): Promise<boolean> {
    if (isRunning || updating) return false;
    setUpdating(true);
    setError(null);
    try {
      const updated = await updateSession(session.id, changes);
      setSessions((current) => current.map((item) => item.id === updated.id ? updated : item));
      if (selectedSession?.id === session.id) {
        if (changes.archived === true) {
          detailRequestId.current += 1;
          setSelectedSession(null);
          setMessages([]);
          setRunPhase("idle");
        } else setSelectedSession(updated);
      }
      return true;
    } catch (caught) {
      setError(errorMessage(caught));
      return false;
    } finally {
      setUpdating(false);
    }
  }

  const statusLabel = useMemo(() => phaseLabel(runPhase), [runPhase]);

  return (
    <div className="app-shell" data-theme={theme}>
      <aside className={`sidebar ${sidebarOpen ? "sidebar--open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">
            d<span>·</span>c
          </div>
          <div>
            <div className="brand-name">dotClaw</div>
            <div className="brand-caption">你的本地 AI 工作区</div>
          </div>
          <button
            className="sidebar-close"
            type="button"
            aria-label="关闭会话列表"
            onClick={() => setSidebarOpen(false)}
          >
            ×
          </button>
        </div>

        <button
          className="new-session"
          type="button"
          disabled={creating || isRunning || updating}
          onClick={() => void handleRequestCreateSession()}
        >
          <span aria-hidden="true">＋</span>
          {creating ? "正在创建" : "新建会话"}
        </button>

        <SessionList
          sessions={sessions}
          selectedId={selectedSession?.id}
          disabled={isRunning || updating || creating}
          onSelect={(session) => void openSession(session)}
          onUpdate={handleUpdateSession}
        />

        <div className="runtime-note">
          <span className="status-dot" />
          <div>
            <strong>仅本机访问</strong>
            <span>127.0.0.1 · 数据保留在本地</span>
          </div>
        </div>
      </aside>

      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          type="button"
          aria-label="关闭会话列表"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <main className="workspace">
        <header className="workspace-header">
          <button
            className="menu-button"
            type="button"
            aria-label="打开会话列表"
            onClick={() => setSidebarOpen(true)}
          >
            <span />
            <span />
          </button>
          <div className="session-context">
            <h1>{selectedSession?.title ?? "本地工作区"}</h1>
            {selectedSession && (
              <p>
                {selectedSession.agent_id} <span>/</span> {selectedSession.model}
              </p>
            )}
          </div>
          <div className={`run-state run-state--${runPhase}`} aria-live="polite">
            <span />
            {statusLabel}
          </div>
          <button
            className="theme-button"
            type="button"
            aria-label={theme === "light" ? "切换深色外观" : "切换浅色外观"}
            title={theme === "light" ? "切换深色外观" : "切换浅色外观"}
            onClick={() => setTheme(theme === "light" ? "dark" : "light")}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              {theme === "light" ? (
                <path d="M20 15.5A8.5 8.5 0 0 1 8.5 4 8.5 8.5 0 1 0 20 15.5Z" />
              ) : (
                <><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" /></>
              )}
            </svg>
          </button>
        </header>

        <section className="conversation" aria-label="对话内容" ref={conversationRef} onScroll={onScroll}>
          {loading ? (
            <LoadingState />
          ) : selectedSession === null ? (
            <EmptyState hasSessions={sessions.length > 0} onCreate={() => void handleRequestCreateSession()} />
          ) : messages.length === 0 && runActivity === null ? (
            <WelcomeState />
          ) : (
            <div className="message-column">
              {messages.map((message) => (
                <Message key={message.id} message={message} />
              ))}
              {runActivity && (
                <RunActivityPanel
                  activity={runActivity}
                  busy={isRunning}
                  onApprove={handleApproval}
                  onResumeDelegation={handleResumeDelegation}
                  onResumeRun={handleResumeRun}
                />
              )}
            </div>
          )}
        </section>

        <div className="composer-region">
          {awayFromBottom && <button className="scroll-bottom" type="button" onClick={scrollToBottom}>{hasNewContent ? "有新内容 · 回到底部 ↓" : "回到底部 ↓"}</button>}
          {error && (
            <div className="error-strip" role="alert">
              <span>!</span>
              <p>{error}</p>
              <button type="button" onClick={() => setError(null)} aria-label="关闭错误">
                ×
              </button>
            </div>
          )}
          <form className="composer" onSubmit={(event) => void handleSubmit(event)}>
            <label className="sr-only" htmlFor="message-input">
              输入消息
            </label>
            <textarea
              id="message-input"
              value={draft}
              rows={1}
              disabled={selectedSession === null || isRunning || updating}
              placeholder={selectedSession ? "输入消息，Enter 发送" : "请先创建一个会话"}
              onChange={(event) => setDraft(event.target.value)}
              onCompositionStart={() => { composing.current = true; }}
              onCompositionEnd={() => { composing.current = false; }}
              onKeyDown={handleComposerKeyDown}
            />
            {isRunning ? (
              <button
                className="composer-action composer-action--stop"
                type="button"
                disabled={runPhase === "stopping"}
                aria-label="停止运行"
                onClick={() => void handleStop()}
              >
                <span />
              </button>
            ) : (
              <button
                className="composer-action"
                type="submit"
                disabled={selectedSession === null || !draft.trim() || updating}
                aria-label="发送消息"
              >
                <ArrowIcon />
              </button>
            )}
          </form>
          <div className="composer-hint">
            <span>Enter 发送 · Shift + Enter 换行</span>
            <span>{storageFailed ? "草稿暂未保存到浏览器" : "草稿自动保存在此浏览器"}</span>
          </div>
        </div>
      </main>
      {showCreateDialog && createOptions && (
        <CreateSessionDialog
          options={createOptions}
          busy={creating}
          onCancel={() => setShowCreateDialog(false)}
          onCreate={(agentId, model) => void handleCreateSession(agentId, model)}
        />
      )}
    </div>
  );
}

function Message({ message }: { message: DisplayMessage }) {
  const isAssistant = message.role === "assistant";
  return (
    <article className={`message message--${message.role}`}>
      <div className="message-author">
        <span>{isAssistant ? "dotClaw" : "你"}</span>
        <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
      </div>
      <div className="message-body">
        {isAssistant && message.reasoning && (
          <details className="reasoning" open={message.pending || undefined}>
            <summary>思考过程</summary>
            <p>{message.reasoning}</p>
          </details>
        )}
        <div className="message-content">
          {message.content ? (
            isAssistant ? (
              <MessageContent content={message.content} />
            ) : (
              message.content
            )
          ) : message.pending ? (
            "正在组织回答"
          ) : (
            "运行未返回内容"
          )}
          {message.pending && <span className="stream-cursor" aria-hidden="true" />}
        </div>
        {isAssistant && message.content && !message.pending && <div className="message-actions"><CopyButton label="复制回答" getText={() => message.content} /></div>}
      </div>
    </article>
  );
}

function EmptyState({ onCreate, hasSessions }: { onCreate: () => void; hasSessions: boolean }) {
  return (
    <div className="empty-state">
      <div className="welcome-mark" aria-hidden="true">d<span>·</span>c</div>
      <p>{hasSessions ? "选择已有会话，或开始新的对话" : "当前还没有会话"}</p>
      <h2>从一个清晰的问题开始。</h2>
      <button type="button" onClick={onCreate}>
        {hasSessions ? "创建新会话" : "创建第一个会话"} <ArrowIcon />
      </button>
    </div>
  );
}

function WelcomeState() {
  return (
    <div className="welcome-state">
      <div className="welcome-mark" aria-hidden="true">d<span>·</span>c</div>
      <p>一切就绪，开始新的对话</p>
      <h2>把任务交给 dotClaw。</h2>
      <span>回答会在这里实时出现，运行期间可以随时停止。</span>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="loading-state" aria-label="正在加载工作区">
      <span />
      <span />
      <span />
    </div>
  );
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M4 10h11M11 5l5 5-5 5" />
    </svg>
  );
}

function phaseLabel(phase: RunPhase): string {
  const labels: Record<RunPhase, string> = {
    idle: "就绪",
    running: "运行中",
    stopping: "正在停止",
    completed: "已完成",
    failed: "运行失败",
    cancelled: "已停止",
    suspended: "已挂起",
    recoverable: "可恢复",
  };
  return labels[phase];
}

function stringField(data: Record<string, unknown>, field: string): string {
  const value = data[field];
  return typeof value === "string" ? value : "";
}

function nestedStringField(
  data: Record<string, unknown>,
  parent: string,
  field: string,
): string {
  const value = data[parent];
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return "";
  }
  return stringField(value as Record<string, unknown>, field);
}

function errorMessage(caught: unknown): string {
  if (caught instanceof ApiError || caught instanceof Error) {
    return caught.message;
  }
  return "发生未知错误，请稍后重试";
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
