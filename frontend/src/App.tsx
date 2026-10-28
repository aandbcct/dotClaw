import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, createSession, deleteSession, getRunActivity, getRuntimeSettings, getSession, getWorkspaceOptions, reconnectMcp, resolveApproval, resumeDelegation, resumeRun, switchSessionModel, toggleSkill, toggleTool, updateSession } from "./api";
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
import { phaseLabel, useSessionWorkspace } from "./useSessionWorkspace";
import type { DisplayMessage, RunActivity, RuntimeSettings, SearchHit, SessionSummary, WorkspaceOptions } from "./types";

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
  const [inspector, setInspector] = useState<{ sessionId: string; runIds: string[]; activity: RunActivity | null; error: string } | null>(null);
  const inspectorRequest = useRef(0);
  const [targetMessage, setTargetMessage] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const { conversationRef, onScroll, scrollToBottom, awayFromBottom, hasNewContent } = useConversationScroll(selectedId || undefined, view.messages);
  const busy = workspace.isStreaming(selectedId) || ["running", "stopping", "suspended", "recoverable"].includes(view.phase);
  const runLabel = view.detached ? "结果待确认" : view.phase !== "running" ? phaseLabel(view.phase) : view.error ? "运行异常" : view.submissionStage === "sending" ? "正在发送" : view.submissionStage === "accepted" ? "已接收，正在处理" : phaseLabel(view.phase);
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
    setMobileNav(false); setInspector(null); setModelOpen(false); setModelError(""); setNewError("");
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
  function restoreQuestion(): void {
    if (!view.lastSubmittedText) return;
    setDraft(draft.trim() ? draft + "\n\n" + view.lastSubmittedText : view.lastSubmittedText);
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
  async function showRun(sessionId: string, runIds: string[], chosen = runIds[0]): Promise<void> {
    const request = ++inspectorRequest.current;
    setInspector({ sessionId, runIds, activity: null, error: "" });
    try {
      const activity = await getRunActivity(sessionId, chosen);
      setInspector((current) => request === inspectorRequest.current && current?.sessionId === sessionId ? { ...current, activity, error: activity ? "" : "该运行记录不存在或不属于此会话" } : current);
    } catch (error) { setInspector((current) => request === inspectorRequest.current && current ? { ...current, error: errorText(error) } : current); }
  }
  const liveInspector = inspector && workspace.views[inspector.sessionId]?.activity;
  const detailActivity = liveInspector && liveInspector.run_id === inspector?.activity?.run_id ? liveInspector : inspector?.activity;
  const canControl = Boolean(inspector && detailActivity && workspace.views[inspector.sessionId]?.activity?.run_id === detailActivity.run_id);
  function controlRun(action: "approve" | "reject" | "delegation" | "resume"): void {
    if (!inspector || !detailActivity || !canControl) return;
    const activity = detailActivity; const id = inspector.sessionId;
    if (action === "approve" || action === "reject") {
      if (activity.approval_id) void workspace.control(id, (callback, signal) => resolveApproval(activity.approval_id!, action === "approve", callback, signal));
    } else if (action === "delegation" && activity.child_run_id) void workspace.control(id, (callback, signal) => resumeDelegation(activity.child_run_id!, callback, signal));
    else if (action === "resume") void workspace.control(id, (callback, signal) => resumeRun(activity.run_id, callback, signal));
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
        {["running", "stopping", "suspended", "recoverable", "failed"].includes(view.phase) && <span className={`run-state run-state--${view.phase}`} role="status">{runLabel}</span>}
        <button className="icon-button theme-button" type="button" aria-label={theme === "light" ? "切换深色外观" : "切换浅色外观"} onClick={() => setTheme(theme === "light" ? "dark" : "light")}>{theme === "light" ? "☾" : "☀"}</button>
        {selected && <button className="icon-button" type="button" aria-label="当前会话更多操作" onClick={() => { setMenu(selected); setActionError(""); }}>⋯</button>}
      </header>
      <section className="conversation" aria-label="对话内容" ref={conversationRef} onScroll={onScroll}>
        {workspace.loading || view.loading ? <p className="empty-note muted">正在读取会话…</p> : <div className="message-column">
          {!view.messages.length && !view.activity && <h1 className="welcome-title">有什么可以帮你？</h1>}
          {view.messages.map((message) => <article id={`message-${message.id}`} className={`message message--${message.role} ${highlight === message.id ? "message--highlight" : ""}`} key={message.id}>
            <span className="sr-only">{message.role === "user" ? "你" : "dotClaw"}</span>
            <div className="message-body" title={message.created_at}>
              {message.reasoning && <details className="reasoning"><summary>思考过程</summary><p>{message.reasoning}</p></details>}
              <div className="message-content">{message.role === "assistant" ? !message.pending && !message.content.trim() ? <p className="empty-answer">{message.id.startsWith("local-") ? "本次未获得有效回答" : "该次回答为空"}</p> : <MessageContent content={message.content} /> : message.content}</div>
              {message.pending && <span className="stream-cursor" aria-label="正在生成" />}
            </div>
            {message.role === "assistant" && !message.pending && <div className="message-actions">
              {message.content && <CopyButton getText={() => message.content} label="复制回答" />}
              {message.content && <button type="button" onClick={() => { setDraft(message.content); inputRef.current?.focus(); }}>引用到输入框</button>}
              {Boolean(message.run_ids?.length) && selectedId && <button type="button" onClick={() => void showRun(selectedId, message.run_ids!)}>执行记录</button>}
            </div>}
            {message.role === "assistant" && !message.pending && Boolean(message.run_ids?.length) && selectedId && <SearchSources sessionId={selectedId} runIds={message.run_ids!} />}
          </article>)}
          {view.detached && <p className="status-note">{view.activity?.status === "running" ? "任务仍在运行，完成后显示结果。" : "结果待确认，请查询运行状态。"}当前连接没有可恢复的逐字增量。</p>}
          {view.runId && !view.detached && !view.error && view.phase === "running" && ["accepted", "streaming"].includes(view.submissionStage) && <div className="accepted-run-id"><span>运行 ID：{view.runId}</span><CopyButton label="复制当前运行 ID" getText={() => view.runId || ""} /></div>}
          {view.activity && <div className={`run-summary ${view.activity.status === "waiting_approval" ? "run-summary--approval" : ""}`}>
            <span>{view.activity.status === "waiting_approval" ? "需要你的批准" : `执行记录 · ${phaseLabel(view.phase)} · ${view.activity.tools.length} 个工具`}</span>
            <button type="button" onClick={() => selectedId && void showRun(selectedId, [view.activity!.run_id])}>{view.activity.status === "waiting_approval" ? "查看并决定" : "查看详情"}</button>
          </div>}
        </div>}
      </section>
      <div className="composer-region">
        {(view.error || newError || workspace.loadError || view.statusError) && <div className={`error-strip ${view.detached ? "error-strip--unknown" : ""}`} role="alert">
          {(view.error || newError || workspace.loadError) && <p>{view.error || newError || workspace.loadError}</p>}
          {view.statusError && <p>{view.statusError}</p>}
          {view.runId && <div className="error-run-id"><span>运行 ID：{view.runId}</span><CopyButton label="复制错误运行 ID" getText={() => view.runId || ""} /></div>}
          <div className="error-actions">
            {view.runId && selectedId && <button type="button" onClick={() => void showRun(selectedId, [view.runId!])}>查看执行记录</button>}
            {view.lastSubmittedText && <button type="button" onClick={restoreQuestion}>恢复原问题到输入框</button>}
            {selectedId && <button type="button" onClick={() => void workspace.checkRunStatus(selectedId)}>查询运行状态</button>}
          </div>
        </div>}
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
    {inspector && <Dialog title="执行详情" className="execution-drawer" onClose={() => setInspector(null)}>
      {inspector.runIds.length > 1 && <label>本轮运行<select aria-label="选择运行记录" onChange={(event) => void showRun(inspector.sessionId, inspector.runIds, event.target.value)} value={detailActivity?.run_id || ""}>{inspector.runIds.map((id) => <option key={id}>{id}</option>)}</select></label>}
      {inspector.error && <p className="inline-error" role="alert">{inspector.error}</p>}
      {!detailActivity && !inspector.error && <p className="muted">正在读取执行记录…</p>}
      {detailActivity && <RunActivityPanel activity={detailActivity} busy={!canControl || ["running", "stopping"].includes(workspace.views[inspector.sessionId]?.phase)} onApprove={(approved) => controlRun(approved ? "approve" : "reject")} onResumeDelegation={() => controlRun("delegation")} onResumeRun={() => controlRun("resume")} />}
    </Dialog>}
  </div>;
}
function errorText(error: unknown): string { return error instanceof ApiError || error instanceof Error ? error.message : "请求失败，请稍后再试"; }
