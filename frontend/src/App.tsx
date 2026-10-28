import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, createSession, deleteSession, getRuntimeSettings, getSession, getWorkspaceOptions, reconnectMcp, resolveApproval, resumeDelegation, resumeRun, switchSessionModel, toggleSkill, toggleTool, updateSession } from "./api";
import { CreateSessionDialog } from "./CreateSessionDialog";
import { Dialog } from "./Dialog";
import { CopyButton, MessageContent } from "./MessageContent";
import { RunActivityPanel } from "./RunActivityPanel";
import { SearchDialog } from "./SearchDialog";
import { SearchSources } from "./SearchSources";
import { SessionList, SessionMenu } from "./SessionList";
import { SettingsPanel } from "./SettingsPanel";
import { exportMarkdown, readPreference, writePreference } from "./preferences";
import { useConversationScroll } from "./useConversationScroll";
import { useDraft } from "./useDraft";
import { useSessionWorkspace } from "./useSessionWorkspace";
import type { RuntimeSettings, SearchHit, SessionSummary, WorkspaceOptions } from "./types";

export function App() {
  const workspace = useSessionWorkspace();
  const { selected, selectedId, view } = workspace;
  const { draft, setDraft, storageFailed } = useDraft(selectedId || undefined);
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem("dotclaw.theme") === "dark" ? "dark" : "light"; } catch { return "light"; } });
  const [collapsed, setCollapsed] = useState(() => readPreference("dotclaw.sidebar.v1", window.innerWidth < 1024));
  const [mobileNav, setMobileNav] = useState(false);
  const [pinned, setPinned] = useState<string[]>(() => { const value = readPreference<unknown>("dotclaw.pins.v1", []); return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; });
  const [options, setOptions] = useState<WorkspaceOptions | null>(null);
  const [nextChoice, setNextChoice] = useState<{ agent_id: string; model: string } | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [modelError, setModelError] = useState("");
  const [modelBusy, setModelBusy] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [menu, setMenu] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState<SessionSummary | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [creating, setCreating] = useState(false);
  const [newError, setNewError] = useState("");
  const submitting = useRef(new Set<string>());
  const composing = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsBusy, setSettingsBusy] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState("");
  const [targetMessage, setTargetMessage] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const { conversationRef, onScroll, scrollToBottom, awayFromBottom, hasNewContent } = useConversationScroll(selectedId || undefined, view.messages);
  const busy = workspace.isStreaming(selectedId) || ["running", "stopping", "suspended", "recoverable"].includes(view.phase);
  useEffect(() => { try { localStorage.setItem("dotclaw.theme", theme); } catch { /* 外观仍在本页生效。 */ } }, [theme]);
  useEffect(() => { writePreference("dotclaw.sidebar.v1", collapsed); }, [collapsed]);
  useEffect(() => { writePreference("dotclaw.pins.v1", pinned); }, [pinned]);
  useEffect(() => {
    if (workspace.loading) return;
    setPinned((current) => current.filter((id) => workspace.sessions.some((session) => session.id === id)));
  }, [workspace.loading, workspace.sessions]);
  useEffect(() => {
    if (!targetMessage || view.loading) return;
    const element = document.getElementById(`message-${targetMessage}`);
    if (!element) return;
    element.scrollIntoView({ block: "center" });
    setHighlight(targetMessage); setTargetMessage(null);
  }, [targetMessage, view.loading, view.messages]);
  useEffect(() => {
    if (!highlight) return;
    const timer = window.setTimeout(() => setHighlight(null), 2400);
    return () => window.clearTimeout(timer);
  }, [highlight]);
  async function loadOptions(): Promise<WorkspaceOptions> {
    const value = options || await getWorkspaceOptions();
    if (!value.models.length) throw new Error("没有可用的对话模型，请检查本地模型配置并重启");
    setOptions(value); return value;
  }
  function startNew(): void {
    workspace.startNew(); setMobileNav(false); setNewError(""); setModelError("");
    inputRef.current?.focus();
  }
  async function open(session: SessionSummary): Promise<void> {
    setMobileNav(false); setModelOpen(false); setModelError(""); setNewError("");
    await workspace.open(session);
  }
  async function submit(event?: FormEvent): Promise<void> {
    event?.preventDefault();
    const content = draft.trim();
    const submissionKey = selectedId || "new";
    if (!content || busy || submitting.current.has(submissionKey)) return;
    submitting.current.add(submissionKey);
    let id = selectedId;
    setNewError("");
    try {
      if (!id) {
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
    }
    finally { submitting.current.delete(submissionKey); if (submissionKey === "new") setCreating(false); }
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
    if (!selected) { setNextChoice({ agent_id: nextChoice?.agent_id || options?.default_agent_id || "default", model }); setModelOpen(false); return; }
    setModelBusy(true); setModelError("");
    try { workspace.upsert(await switchSessionModel(selected.id, model)); setModelOpen(false); }
    catch (error) { setModelError(errorText(error)); }
    finally { setModelBusy(false); }
  }
  async function changeSession(changes: { title?: string; archived?: boolean }): Promise<void> {
    if (!menu) return;
    setActionBusy(true); setActionError("");
    try {
      const updated = await updateSession(menu.id, changes); workspace.upsert(updated); setMenu(null);
      if (changes.archived && selectedId === menu.id) workspace.startNew();
    } catch (error) { setActionError(errorText(error)); }
    finally { setActionBusy(false); }
  }
  async function confirmDelete(): Promise<void> {
    if (!deleting) return;
    const session = deleting; setActionBusy(true); setActionError("");
    try {
      await deleteSession(session.id); workspace.remove(session.id); setDraft("", session.id);
      setPinned((current) => current.filter((id) => id !== session.id));
      setDeleting(null);
    } catch (error) { setActionError(errorText(error)); }
    finally { setActionBusy(false); }
  }
  async function exportSession(session: SessionSummary): Promise<void> {
    setActionError("");
    try { exportMarkdown(await getSession(session.id), ["running", "stopping"].includes(workspace.views[session.id]?.phase)); setMenu(null); }
    catch (error) { setActionError(errorText(error)); }
  }
  async function selectSearch(hit: SearchHit): Promise<void> {
    setSearchOpen(false);
    try {
      const session = workspace.sessions.find((item) => item.id === hit.session_id) || await getSession(hit.session_id);
      await open(session); setTargetMessage(hit.message_id);
    } catch (error) { setNewError(errorText(error)); }
  }
  async function loadSettings(): Promise<void> {
    setSettingsOpen(true); setMobileNav(false); setSettingsLoading(true); setSettingsError("");
    try { setSettings(await getRuntimeSettings()); } catch (error) { setSettingsError(errorText(error)); }
    finally { setSettingsLoading(false); }
  }
  async function settingAction(key: string, action: () => Promise<RuntimeSettings>): Promise<void> {
    if (settingsBusy) return;
    setSettingsBusy(key); setSettingsError("");
    try { setSettings(await action()); } catch (error) { setSettingsError(errorText(error)); }
    finally { setSettingsBusy(null); }
  }
  function controlRun(sessionId: string, messageId: string, runId: string, action: "approve" | "reject" | "delegation" | "resume"): void {
    const view = workspace.views[sessionId];
    const activity = view?.activitiesByRunId[runId];
    if (!activity || view.runId !== runId || view.activeMessageId !== messageId) return;
    if ((action === "approve" || action === "reject") && activity.approval_id) void workspace.control(sessionId, messageId, runId, (callback, signal) => resolveApproval(activity.approval_id!, action === "approve", callback, signal));
    else if (action === "delegation" && activity.child_run_id) void workspace.control(sessionId, messageId, runId, (callback, signal) => resumeDelegation(activity.child_run_id!, callback, signal));
    else if (action === "resume") void workspace.control(sessionId, messageId, runId, (callback, signal) => resumeRun(runId, callback, signal));
  }
  const navigation = <>
    <div className="brand-row"><strong className="brand-name">dotClaw</strong><button type="button" aria-label="折叠侧栏" onClick={() => { setCollapsed(!collapsed); setMobileNav(false); }}>◧</button></div>
    <button className="nav-action" type="button" aria-label="新对话" onClick={startNew}><span aria-hidden="true">＋</span><span>新对话</span></button>
    <button className="nav-action" type="button" aria-label="搜索对话" onClick={() => { setSearchOpen(true); setMobileNav(false); }}><span aria-hidden="true">⌕</span><span>搜索对话</span></button>
    <SessionList sessions={workspace.sessions} selectedId={selectedId || undefined} pinned={pinned} activeIds={Object.entries(workspace.views).filter(([, value]) => ["running", "stopping", "suspended", "recoverable"].includes(value.phase)).map(([id]) => id)} onSelect={(session) => void open(session)} onMenu={(session) => { setActionError(""); setMenu(session); }} />
    <button className="nav-action settings-entry" type="button" aria-label="设置" onClick={() => void loadSettings()}><span aria-hidden="true">⚙</span><span>设置</span></button>
  </>;
  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`} data-theme={theme}>
    <aside className="sidebar" aria-label="工作区导航">{navigation}</aside>
    {mobileNav && <Dialog title="会话导航" className="mobile-navigation" onClose={() => setMobileNav(false)}>{navigation}</Dialog>}
    <main className={`workspace ${!selected && !view.messages.length ? "workspace--new" : ""}`}>
      <header className="workspace-header">
        <button className="menu-button" type="button" aria-label="打开会话列表" onClick={() => setMobileNav(true)}>☰</button>
        <div className="model-context">
          <button className="model-selector" type="button" disabled={modelBusy || ["running", "stopping"].includes(view.phase)} onClick={() => void requestModel()}>{selected?.model || nextChoice?.model || "dotClaw"} <span aria-hidden="true">⌄</span></button>
          {modelError && !modelOpen && <span className="inline-error" role="alert">{modelError}</span>}
        </div>
        <span className="header-title" title={selected?.title}>{selected?.title || "新对话"}</span>
        <button className="icon-button theme-button" type="button" aria-label={theme === "light" ? "切换深色外观" : "切换浅色外观"} onClick={() => setTheme(theme === "light" ? "dark" : "light")}>{theme === "light" ? "☾" : "☀"}</button>
        {selected && <button className="icon-button" type="button" aria-label="当前会话更多操作" onClick={() => { setMenu(selected); setActionError(""); }}>⋯</button>}
      </header>
      <section className="conversation" aria-label="对话内容" ref={conversationRef} onScroll={onScroll}>
        {workspace.loading || view.loading ? <p className="empty-note muted">正在读取会话…</p> : <div className="message-column">
          {!view.messages.length && !view.activity && <h1 className="welcome-title">有什么可以帮你？</h1>}
          {view.messages.map((message) => <article id={`message-${message.id}`} className={`message message--${message.role} ${highlight === message.id ? "message--highlight" : ""}`} key={message.id}>
            <span className="sr-only">{message.role === "user" ? "你" : "dotClaw"}</span>
            <div className="message-body" title={message.created_at}>
              {message.role === "assistant" ? <RunActivityPanel message={message} activities={(message.run_ids || []).map((id) => view.activitiesByRunId[id]).filter((activity) => Boolean(activity))} canControl={view.activeMessageId === message.id && !workspace.isStreaming(selectedId) && ["suspended", "recoverable"].includes(view.phase)}
                onLoadRun={(runId) => selectedId && void workspace.loadRunActivity(selectedId, runId)} onCheckStatus={(runId) => selectedId && void workspace.checkRunStatus(selectedId, runId)} onRestoreInput={() => restoreQuestion(message.execution?.submittedText || "")}
                onApprove={(runId, approved) => selectedId && controlRun(selectedId, message.id, runId, approved ? "approve" : "reject")}
                onResumeDelegation={(runId) => selectedId && controlRun(selectedId, message.id, runId, "delegation")} onResumeRun={(runId) => selectedId && controlRun(selectedId, message.id, runId, "resume")}>
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
        </div>}
      </section>
      <div className="composer-region">
        {(newError || workspace.loadError || (!view.activeMessageId && view.error)) && <div className="error-strip" role="alert"><p>{newError || workspace.loadError || view.error}</p></div>}
        <form className="composer" onSubmit={(event) => void submit(event)}>
          <label className="sr-only" htmlFor="message-input">输入消息</label>
          <textarea ref={inputRef} id="message-input" value={draft} placeholder="输入消息…" rows={2} onChange={(event) => setDraft(event.target.value)} onKeyDown={keyDown} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} />
          {["running", "stopping"].includes(view.phase) && (!view.detached || view.activity?.status === "running") ? <button className="composer-action" type="button" aria-label="停止运行" disabled={view.phase === "stopping"} onClick={() => selectedId && void workspace.stop(selectedId)}>■</button> : <button className="composer-action" aria-label="发送消息" type="submit" disabled={!draft.trim() || busy || (creating && !selectedId)}>↑</button>}
        </form>
        <div className="composer-hint"><span>Enter 发送 · Shift + Enter 换行</span>{!selected && <button type="button" onClick={() => void requestAdvanced()}>智能体与模型</button>}{storageFailed && <span role="alert">草稿保存失败</span>}</div>
      </div>
      {awayFromBottom && <button className="back-to-bottom" type="button" onClick={scrollToBottom}>{hasNewContent ? "有新内容 · 回到底部" : "回到底部"} ↓</button>}
    </main>
    {advanced && options && <CreateSessionDialog options={options} onCancel={() => setAdvanced(false)} onCreate={(agent_id, model) => { setNextChoice({ agent_id, model }); setAdvanced(false); }} />}
    {modelOpen && options && <Dialog title="选择模型" className="model-dialog" onClose={() => setModelOpen(false)}>
      <p className="muted">用于后续新消息；当前运行保持原模型。智能体：{selected?.agent_id || nextChoice?.agent_id || options.default_agent_id}</p>
      <div className="menu-actions">{options.models.map((model) => <button type="button" aria-pressed={(selected?.model || nextChoice?.model || options.default_model) === model} disabled={modelBusy} key={model} onClick={() => void changeModel(model)}>{model}</button>)}</div>
      {modelError && <p className="inline-error" role="alert">{modelError}</p>}
    </Dialog>}
    {searchOpen && <SearchDialog onClose={() => setSearchOpen(false)} onSelect={(hit) => void selectSearch(hit)} />}
    {menu && <SessionMenu session={menu} pinned={pinned.includes(menu.id)} busy={actionBusy} error={actionError} onClose={() => setMenu(null)} onUpdate={changeSession} onPin={() => { setPinned((current) => current.includes(menu.id) ? current.filter((id) => id !== menu.id) : [...current, menu.id]); setMenu(null); }} onExport={() => void exportSession(menu)} onDelete={() => { setDeleting(menu); setMenu(null); setActionError(""); }} />}
    {deleting && <Dialog title="删除会话" onClose={() => { if (!actionBusy) setDeleting(null); }}>
      <p>删除“{deleting.title}”及其运行记录，无法恢复。</p>
      {actionError && <p className="inline-error" role="alert">{actionError}</p>}
      <footer className="dialog-actions"><button data-initial-focus autoFocus type="button" className="secondary-button" disabled={actionBusy} onClick={() => setDeleting(null)}>取消</button>{actionError && <button type="button" onClick={() => { void open(deleting); setDeleting(null); }}>打开会话处理运行</button>}<button type="button" className="danger-button" disabled={actionBusy} onClick={() => void confirmDelete()}>确认删除</button></footer>
    </Dialog>}
    {settingsOpen && <SettingsPanel settings={settings} loading={settingsLoading} busyKey={settingsBusy} error={settingsError} onRetry={() => void loadSettings()} onClose={() => setSettingsOpen(false)} onToggleTool={(name, enabled) => void settingAction(name, () => toggleTool(name, enabled))} onToggleSkill={(name, enabled) => void settingAction(name, () => toggleSkill(name, enabled))} onReconnectMcp={() => void settingAction("mcp", reconnectMcp)} />}

  </div>;
}
function errorText(error: unknown): string { return error instanceof ApiError || error instanceof Error ? error.message : "请求失败，请稍后再试"; }
