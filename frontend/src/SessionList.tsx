import { useState } from "react";
import type { SessionSummary } from "./types";
import { Dialog } from "./Dialog";

interface Props {
  sessions: SessionSummary[]; selectedId?: string; pinned: string[]; activeIds: string[];
  onSelect: (session: SessionSummary) => void; onMenu: (session: SessionSummary) => void;
}

/** 标题单行呈现，低频管理操作收纳到键盘和触摸均可访问的菜单。 */
export function SessionList({ sessions, selectedId, pinned, activeIds, onSelect, onMenu }: Props) {
  const [archived, setArchived] = useState(false);
  const visible = sessions.filter((session) => Boolean(session.archived) === archived);
  const groups = new Map<string, SessionSummary[]>();
  for (const session of visible) {
    const age = (Date.now() - new Date(session.updated_at).getTime()) / 86400000;
    const group = pinned.includes(session.id) ? "置顶" : age < 1 ? "今天" : age < 7 ? "最近 7 天" : "更早";
    groups.set(group, [...(groups.get(group) || []), session]);
  }
  return <>
    <nav className="session-list" aria-label="会话列表">
      {visible.length === 0 && <p className="muted empty-note">{archived ? "暂无归档会话" : "暂无会话"}</p>}
      {["置顶", "今天", "最近 7 天", "更早"].map((group) => groups.has(group) && <section key={group}>
        <h2 className="session-group">{group}</h2>
        {groups.get(group)?.map((session) => <div className={`session-row ${selectedId === session.id ? "session-row--active" : ""}`} key={session.id}>
          <button className="session-item" type="button" title={session.title} aria-current={selectedId === session.id ? "page" : undefined} onClick={() => onSelect(session)}>
            {activeIds.includes(session.id) && <span className="activity-dot" role="img" aria-label="运行未结束" />}
            <span>{session.title}</span>
          </button>
          <button className="session-more" type="button" aria-label={`更多操作：${session.title}`} onClick={() => onMenu(session)}>⋯</button>
        </div>)}
      </section>)}
    </nav>
    <button className="nav-action archive-entry" type="button" aria-pressed={archived} onClick={() => setArchived(!archived)}><span aria-hidden="true">▣</span><span>{archived ? "返回最近会话" : "已归档"}</span></button>
  </>;
}

export function SessionMenu({ session, pinned, busy, error, onClose, onUpdate, onPin, onExport, onDelete }: {
  session: SessionSummary; pinned: boolean; busy: boolean; error: string; onClose: () => void;
  onUpdate: (changes: { title?: string; archived?: boolean }) => Promise<void>;
  onPin: () => void; onExport: () => void; onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title);
  return <Dialog title="会话操作" className="session-menu" onClose={onClose}>
    <p className="menu-session-title">{session.title}</p>
    {editing ? <form onSubmit={(event) => { event.preventDefault(); if (title.trim()) void onUpdate({ title: title.trim() }); }}>
      <label>会话新标题<input autoFocus aria-label="会话新标题" value={title} onChange={(event) => setTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && event.nativeEvent.isComposing) event.preventDefault(); }} /></label>
      <button className="primary-button" disabled={busy || !title.trim()}>保存</button>
    </form> : <div className="menu-actions">
      <button type="button" onClick={() => setEditing(true)}>重命名</button>
      <button type="button" onClick={onPin}>{pinned ? "取消置顶" : "置顶"}</button>
      <button type="button" disabled={busy} onClick={() => void onUpdate({ archived: !session.archived })}>{session.archived ? "恢复会话" : "归档"}</button>
      <button type="button" disabled={busy} onClick={onExport}>导出 Markdown</button>
      <button type="button" className="danger-action" onClick={onDelete}>删除会话</button>
    </div>}
    {error && <p className="inline-error" role="alert">{error}</p>}
  </Dialog>;
}
