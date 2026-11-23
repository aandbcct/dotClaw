import { useCallback, useEffect, useMemo, useState } from "react";
import { deleteSession, getRuntimeSettings, getSession, reconnectMcp, toggleSkill, toggleTool, updateSession } from "./api";
import { AssistantWorkspace, ASSISTANT_PREFERENCE_KEY } from "./AssistantWorkspace";
import { ChatWorkspace } from "./ChatWorkspace";
import { Dialog } from "./Dialog";
import { NotificationCenter } from "./NotificationCenter";
import { PersonalTasks } from "./PersonalTasks";
import { SearchDialog } from "./SearchDialog";
import { SessionList, SessionMenu } from "./SessionList";
import { SettingsPanel } from "./SettingsPanel";
import { BrowserNotificationDelivery } from "./notificationDelivery";
import { useDraft } from "./useDraft";
import { useNotifications } from "./useNotifications";
import { useSessionWorkspace } from "./useSessionWorkspace";
import { exportMarkdown, readPreference, writePreference } from "./preferences";
import type { RuntimeSettings, SearchHit, SessionSummary } from "./types";

type ActiveWorkspace = "chat" | "assistant" | "tasks" | "notifications";

export function App() {
  const workspace = useSessionWorkspace();
  const assistantWorkspace = useSessionWorkspace({ preferenceKey: ASSISTANT_PREFERENCE_KEY, autoLoad: false });
  const { selected, selectedId } = workspace;
  const { draft, setDraft, storageFailed } = useDraft(selectedId || undefined);
  const [theme, setTheme] = useState<"light" | "dark">(() => { try { return localStorage.getItem("dotclaw.theme") === "dark" ? "dark" : "light"; } catch { return "light"; } });
  const [collapsed, setCollapsed] = useState(() => readPreference("dotclaw.sidebar.v1", window.innerWidth < 1024));
  const [mobileNav, setMobileNav] = useState(false);
  const [activeWorkspace, setActiveWorkspace] = useState<ActiveWorkspace>("chat");
  const [chatResetKey, setChatResetKey] = useState(0);
  const [focusTaskId, setFocusTaskId] = useState<string | null>(null);
  const [focusNotificationId, setFocusNotificationId] = useState<number | null>(null);
  const [taskRefreshRevision, setTaskRefreshRevision] = useState(0);
  const [targetMessage, setTargetMessage] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const handleTargetMessageHandled = useCallback(() => setTargetMessage(null), []);
  const handleHighlighted = useCallback((id: string) => setHighlight(id || null), []);
  const delivery = useMemo(() => new BrowserNotificationDelivery(), []);
  const notifications = useNotifications(delivery, (item) => {
    setActiveWorkspace("tasks");
    if (item.kind === "reminder" && item.members[0]) setFocusTaskId(item.members[0].task_id);
    if (item.kind === "recovery_summary") setFocusNotificationId(item.id);
  });
  const [pinned, setPinned] = useState<string[]>(() => {
    const value = readPreference<unknown>("dotclaw.pins.v1", []);
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  });
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [menu, setMenu] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState<SessionSummary | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsBusy, setSettingsBusy] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState("");

  useEffect(() => { try { localStorage.setItem("dotclaw.theme", theme); } catch { /* 外观仍在本页生效。 */ } }, [theme]);
  useEffect(() => { writePreference("dotclaw.sidebar.v1", collapsed); }, [collapsed]);
  useEffect(() => { writePreference("dotclaw.pins.v1", pinned); }, [pinned]);
  useEffect(() => {
    if (workspace.loading) return;
    setPinned((current) => current.filter((id) => workspace.sessions.some((session) => session.id === id)));
  }, [workspace.loading, workspace.sessions]);

  function startNew(): void {
    setActiveWorkspace("chat");
    workspace.startNew();
    setMobileNav(false);
    setSearchError("");
    setChatResetKey((value) => value + 1);
  }
  async function open(session: SessionSummary): Promise<void> {
    setActiveWorkspace("chat");
    setMobileNav(false);
    setSearchError("");
    await workspace.open(session);
  }
  async function changeSession(changes: { title?: string; archived?: boolean }): Promise<void> {
    if (!menu) return;
    setActionBusy(true); setActionError("");
    try {
      const updated = await updateSession(menu.id, changes);
      workspace.upsert(updated); setMenu(null);
      if (changes.archived && selectedId === menu.id) workspace.startNew();
    } catch (error) { setActionError(error instanceof Error ? error.message : "修改会话失败"); }
    finally { setActionBusy(false); }
  }
  async function confirmDelete(): Promise<void> {
    if (!deleting) return;
    const session = deleting; setActionBusy(true); setActionError("");
    try {
      await deleteSession(session.id); workspace.remove(session.id); setDraft("", session.id);
      setPinned((current) => current.filter((id) => id !== session.id));
      setDeleting(null);
    } catch (error) { setActionError(error instanceof Error ? error.message : "删除会话失败"); }
    finally { setActionBusy(false); }
  }
  async function exportSession(session: SessionSummary): Promise<void> {
    setActionError("");
    try { exportMarkdown(await getSession(session.id), ["running", "stopping"].includes(workspace.views[session.id]?.phase)); setMenu(null); }
    catch (error) { setActionError(error instanceof Error ? error.message : "导出会话失败"); }
  }
  async function selectSearch(hit: SearchHit): Promise<void> {
    setSearchOpen(false);
    setSearchError("");
    try {
      const session = workspace.sessions.find((item) => item.id === hit.session_id) || await getSession(hit.session_id);
      await open(session);
      setTargetMessage(hit.message_id);
    } catch (error) { setSearchError(error instanceof Error ? error.message : "读取搜索结果失败"); }
  }
  async function loadSettings(): Promise<void> {
    setSettingsOpen(true); setMobileNav(false); setSettingsLoading(true); setSettingsError("");
    try { setSettings(await getRuntimeSettings()); }
    catch (error) { setSettingsError(error instanceof Error ? error.message : "读取设置失败"); }
    finally { setSettingsLoading(false); }
  }
  async function settingAction(key: string, action: () => Promise<RuntimeSettings>): Promise<void> {
    if (settingsBusy) return;
    setSettingsBusy(key); setSettingsError("");
    try { setSettings(await action()); }
    catch (error) { setSettingsError(error instanceof Error ? error.message : "更新设置失败"); }
    finally { setSettingsBusy(null); }
  }

  const navigation = <>
    <div className="brand-row"><strong className="brand-name">dotClaw</strong><button type="button" aria-label="折叠侧栏" onClick={() => { setCollapsed(!collapsed); setMobileNav(false); }}>◧</button></div>
    <button className="nav-action" type="button" aria-label="新对话" onClick={startNew}><span aria-hidden="true">＋</span><span>新对话</span></button>
    <button className="nav-action" type="button" aria-label="搜索对话" onClick={() => { setSearchOpen(true); setMobileNav(false); }}><span aria-hidden="true">⌕</span><span>搜索对话</span></button>
    <button className="nav-action" type="button" aria-label="个人助理" aria-pressed={activeWorkspace === "assistant"} onClick={() => { setActiveWorkspace("assistant"); setMobileNav(false); }}><span aria-hidden="true">✦</span><span>个人助理</span></button>
    <button className="nav-action" type="button" aria-label="事务" onClick={() => { setActiveWorkspace("tasks"); setMobileNav(false); }}><span aria-hidden="true">✓</span><span>事务</span></button>
    <button className="nav-action" type="button" aria-label={`通知${notifications.unreadCount ? `，${notifications.unreadCount} 条未读` : ""}`} onClick={() => { setActiveWorkspace("notifications"); setMobileNav(false); }}><span aria-hidden="true">♢</span><span>通知 {notifications.unreadCount > 0 ? `(${notifications.unreadCount})` : ""}</span></button>
    <SessionList sessions={workspace.sessions} selectedId={selectedId || undefined} pinned={pinned}
      activeIds={Object.entries(workspace.views).filter(([, value]) => ["running", "stopping", "suspended", "recoverable"].includes(value.phase)).map(([id]) => id)}
      onSelect={(session) => void open(session)} onMenu={(session) => { setActionError(""); setMenu(session); }} />
    <button className="nav-action settings-entry" type="button" aria-label="设置" onClick={() => void loadSettings()}><span aria-hidden="true">⚙</span><span>设置</span></button>
  </>;

  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`} data-theme={theme}>
    <aside className="sidebar" aria-label="工作区导航">{navigation}</aside>
    {mobileNav && <Dialog title="会话导航" className="mobile-navigation" onClose={() => setMobileNav(false)}>{navigation}</Dialog>}
    <main className={`workspace ${activeWorkspace === "chat" && !selected && !workspace.view.messages.length ? "workspace--new" : ""}`}>
      {activeWorkspace === "tasks" && <PersonalTasks onOpenNavigation={() => setMobileNav(true)} focusTaskId={focusTaskId} focusNotificationId={focusNotificationId} refreshRevision={taskRefreshRevision} onFocusHandled={() => { setFocusTaskId(null); setFocusNotificationId(null); }} />}
      {activeWorkspace === "notifications" && <NotificationCenter onOpenNavigation={() => setMobileNav(true)} revision={notifications.revision} unreadCount={notifications.unreadCount} connectionError={notifications.connectionError} permission={notifications.permission} permissionPending={notifications.permissionPending} permissionError={notifications.permissionError} onEnableSystemNotifications={() => void notifications.enableSystemNotifications()} onRefresh={notifications.refresh} onOpenTask={(id) => { setFocusTaskId(id); setActiveWorkspace("tasks"); }} onOpenSummary={(id) => { setFocusNotificationId(id); setActiveWorkspace("tasks"); }} />}
      <div className="workspace-panel" hidden={activeWorkspace !== "chat"}>
        <ChatWorkspace workspace={workspace} active={activeWorkspace === "chat"} resetErrorKey={chatResetKey} draft={draft} setDraft={setDraft} storageFailed={storageFailed} theme={theme} onToggleTheme={() => setTheme(theme === "light" ? "dark" : "light")}
          onOpenNavigation={() => setMobileNav(true)} onOpenSessionMenu={(session) => { setActionError(""); setMenu(session); }} externalError={searchError} targetMessage={targetMessage} highlight={highlight}
          onTargetMessageHandled={handleTargetMessageHandled} onHighlighted={handleHighlighted} />
      </div>
      <div className="workspace-panel" hidden={activeWorkspace !== "assistant"}>
        <AssistantWorkspace workspace={assistantWorkspace} active={activeWorkspace === "assistant"} theme={theme} onToggleTheme={() => setTheme(theme === "light" ? "dark" : "light")}
          onOpenNavigation={() => setMobileNav(true)} onFocusTask={(id) => { setFocusTaskId(id); setActiveWorkspace("tasks"); }}
          onOperationsCommitted={() => setTaskRefreshRevision((value) => value + 1)} />
      </div>
    </main>
    {searchOpen && <SearchDialog onClose={() => setSearchOpen(false)} onSelect={(hit) => void selectSearch(hit)} />}
    {menu && <SessionMenu session={menu} pinned={pinned.includes(menu.id)} busy={actionBusy} error={actionError} onClose={() => setMenu(null)} onUpdate={changeSession}
      onPin={() => { setPinned((current) => current.includes(menu.id) ? current.filter((id) => id !== menu.id) : [...current, menu.id]); setMenu(null); }}
      onExport={() => void exportSession(menu)} onDelete={() => { setDeleting(menu); setMenu(null); setActionError(""); }} />}
    {deleting && <Dialog title="删除会话" onClose={() => { if (!actionBusy) setDeleting(null); }}>
      <p>删除“{deleting.title}”及其运行记录，无法恢复。</p>
      {actionError && <p className="inline-error" role="alert">{actionError}</p>}
      <footer className="dialog-actions"><button data-initial-focus autoFocus type="button" className="secondary-button" disabled={actionBusy} onClick={() => setDeleting(null)}>取消</button>{actionError && <button type="button" onClick={() => { void open(deleting); setDeleting(null); }}>打开会话处理运行</button>}<button type="button" className="danger-button" disabled={actionBusy} onClick={() => void confirmDelete()}>确认删除</button></footer>
    </Dialog>}
    {settingsOpen && <SettingsPanel settings={settings} loading={settingsLoading} busyKey={settingsBusy} error={settingsError} onRetry={() => void loadSettings()} onClose={() => setSettingsOpen(false)} onToggleTool={(name, enabled) => void settingAction(name, () => toggleTool(name, enabled))} onToggleSkill={(name, enabled) => void settingAction(name, () => toggleSkill(name, enabled))} onReconnectMcp={() => void settingAction("mcp", reconnectMcp)} />}
  </div>;
}
