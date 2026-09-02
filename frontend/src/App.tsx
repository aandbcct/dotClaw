import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import Markdown from "react-markdown";

import {
  ApiError,
  cancelSession,
  createSession,
  getSession,
  listSessions,
  submitMessage,
} from "./api";
import type {
  DisplayMessage,
  RunPhase,
  SessionDetail,
  SessionSummary,
  StreamEvent,
} from "./types";

const TERMINAL_EVENTS = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.suspended",
  "stream.error",
]);

export function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSession, setSelectedSession] = useState<SessionSummary | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [runPhase, setRunPhase] = useState<RunPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const detailRequestId = useRef(0);
  const streamController = useRef<AbortController | null>(null);
  const messageEnd = useRef<HTMLDivElement | null>(null);

  const isRunning = runPhase === "running" || runPhase === "stopping";

  useEffect(() => {
    void loadWorkspace();
    return () => streamController.current?.abort();
  }, []);

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function loadWorkspace(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const availableSessions = await listSessions();
      setSessions(availableSessions);
      if (availableSessions.length > 0) {
        await openSession(availableSessions[0]);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }

  async function openSession(session: SessionSummary): Promise<void> {
    if (isRunning) {
      return;
    }
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setSelectedSession(session);
    setMessages([]);
    setError(null);
    setRunPhase("idle");
    setSidebarOpen(false);
    try {
      const detail = await getSession(session.id);
      if (detailRequestId.current === requestId) {
        setSelectedSession(detail);
        setMessages(detail.messages);
      }
    } catch (caught) {
      if (detailRequestId.current === requestId) {
        setError(errorMessage(caught));
      }
    }
  }

  async function handleCreateSession(): Promise<void> {
    if (creating || isRunning) {
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const created = await createSession();
      setSessions((current) => [created, ...current]);
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
    if (selectedSession === null || !content || isRunning) {
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
    setError(null);
    setRunPhase("running");

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
      const finalContent = nestedStringField(streamEvent.data, "final_message", "content");
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantMessageId && !message.content
            ? { ...message, content: finalContent }
            : message,
        ),
      );
      setRunPhase("completed");
      return;
    }
    if (streamEvent.event === "run.cancelled") {
      setRunPhase("cancelled");
      return;
    }
    if (streamEvent.event === "run.suspended") {
      setRunPhase("suspended");
      setError("运行已挂起，需要返回 CLI 完成后续操作");
      return;
    }

    setRunPhase("failed");
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
    } catch {
      // 流式回答已经完整展示，刷新投影失败不覆盖本次成功结果。
    }
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
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
    }
  }

  const statusLabel = useMemo(() => phaseLabel(runPhase), [runPhase]);

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "sidebar--open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">
            d<span>·</span>c
          </div>
          <div>
            <div className="brand-name">dotClaw</div>
            <div className="brand-caption">LOCAL RUNTIME</div>
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
          disabled={creating || isRunning}
          onClick={() => void handleCreateSession()}
        >
          <span aria-hidden="true">＋</span>
          {creating ? "正在创建" : "新建会话"}
        </button>

        <div className="session-heading">
          <span>最近会话</span>
          <span>{sessions.length.toString().padStart(2, "0")}</span>
        </div>
        <nav className="session-list" aria-label="会话列表">
          {sessions.map((session) => (
            <button
              className={`session-item ${
                selectedSession?.id === session.id ? "session-item--active" : ""
              }`}
              type="button"
              key={session.id}
              disabled={isRunning}
              onClick={() => void openSession(session)}
            >
              <span className="session-title">{session.title}</span>
              <span className="session-meta">
                <span>{session.model || "未选择模型"}</span>
                <span>{relativeTime(session.updated_at)}</span>
              </span>
            </button>
          ))}
        </nav>

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
        </header>

        <section className="conversation" aria-label="对话内容">
          {loading ? (
            <LoadingState />
          ) : selectedSession === null ? (
            <EmptyState onCreate={() => void handleCreateSession()} />
          ) : messages.length === 0 ? (
            <WelcomeState />
          ) : (
            <div className="message-column">
              {messages.map((message) => (
                <Message key={message.id} message={message} />
              ))}
              <div ref={messageEnd} />
            </div>
          )}
        </section>

        <div className="composer-region">
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
              disabled={selectedSession === null || isRunning}
              placeholder={selectedSession ? "输入消息，Enter 发送" : "请先创建一个会话"}
              onChange={(event) => setDraft(event.target.value)}
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
                disabled={selectedSession === null || !draft.trim()}
                aria-label="发送消息"
              >
                <ArrowIcon />
              </button>
            )}
          </form>
          <div className="composer-hint">
            <span>Enter 发送 · Shift + Enter 换行</span>
            <span>刷新后仅恢复已完成对话</span>
          </div>
        </div>
      </main>
    </div>
  );
}

function Message({ message }: { message: DisplayMessage }) {
  const isAssistant = message.role === "assistant";
  return (
    <article className={`message message--${message.role}`}>
      <div className="message-author">
        <span>{isAssistant ? "DC" : "YOU"}</span>
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
              <Markdown>{message.content}</Markdown>
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
      </div>
    </article>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="empty-state">
      <div className="empty-index">01</div>
      <p>当前还没有会话</p>
      <h2>从一个清晰的问题开始。</h2>
      <button type="button" onClick={onCreate}>
        创建第一个会话 <ArrowIcon />
      </button>
    </div>
  );
}

function WelcomeState() {
  return (
    <div className="welcome-state">
      <div className="welcome-rule" />
      <p>SESSION READY</p>
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

function relativeTime(value: string): string {
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) {
    return "";
  }
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟`;
  if (minutes < 1_440) return `${Math.floor(minutes / 60)} 小时`;
  return `${Math.floor(minutes / 1_440)} 天`;
}
