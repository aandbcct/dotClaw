import { useState } from "react";
import type { SessionSummary } from "./types";

type Props = {
  sessions: SessionSummary[];
  selectedId?: string;
  disabled: boolean;
  onSelect: (session: SessionSummary) => void;
  onUpdate: (session: SessionSummary, changes: { title?: string; archived?: boolean }) => Promise<boolean>;
};

/** 最近会话与归档共用标题搜索，管理按钮独立于会话选择按钮。 */
export function SessionList({ sessions, selectedId, disabled, onSelect, onUpdate }: Props) {
  const [query, setQuery] = useState("");
  const [archived, setArchived] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const visible = sessions.filter((session) =>
    Boolean(session.archived) === archived && session.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );

  async function rename(session: SessionSummary): Promise<void> {
    if (!title.trim() || disabled) return;
    if (await onUpdate(session, { title: title.trim() })) setEditingId(null);
  }

  return (
    <>
      <input className="session-search" type="search" aria-label="搜索会话标题" placeholder="搜索会话标题…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div className="session-filters" aria-label="会话分类">
        <button type="button" aria-pressed={!archived} onClick={() => setArchived(false)}>最近会话</button>
        <button type="button" aria-pressed={archived} onClick={() => setArchived(true)}>已归档</button>
      </div>
      <nav className="session-list" aria-label="会话列表">
        {visible.length === 0 && <p className="session-empty">{query ? "没有匹配的会话" : archived ? "暂无归档会话" : "暂无会话"}</p>}
        {visible.map((session) => (
          <div className={`session-row ${selectedId === session.id ? "session-row--active" : ""}`} key={session.id}>
            {editingId === session.id ? (
              <form className="session-rename" onSubmit={(event) => { event.preventDefault(); void rename(session); }}>
                <input aria-label="会话新标题" autoFocus value={title} disabled={disabled} onChange={(event) => setTitle(event.target.value)} onKeyDown={(event) => {
                  if (event.key === "Escape") setEditingId(null);
                  if (event.key === "Enter" && event.nativeEvent.isComposing) event.preventDefault();
                }} />
                <div>
                  <button type="submit" disabled={disabled || !title.trim()}>保存</button>
                  <button type="button" disabled={disabled} onClick={() => setEditingId(null)}>取消</button>
                </div>
              </form>
            ) : (
              <>
                <button className="session-item" type="button" disabled={disabled} aria-current={selectedId === session.id ? "page" : undefined} onClick={() => onSelect(session)}>
                  <span className="session-title">{session.title}</span>
                  <span className="session-meta"><span>{session.model || "未选择模型"}</span><time dateTime={session.updated_at}>{formatDate(session.updated_at)}</time></span>
                </button>
                <div className="session-actions">
                  <button type="button" disabled={disabled} aria-label={`重命名会话：${session.title}`} onClick={() => { setEditingId(session.id); setTitle(session.title); }}>重命名</button>
                  <button type="button" disabled={disabled} aria-label={`${archived ? "恢复" : "归档"}会话：${session.title}`} onClick={() => void onUpdate(session, { archived: !archived })}>{archived ? "恢复" : "归档"}</button>
                </div>
              </>
            )}
          </div>
        ))}
      </nav>
    </>
  );
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}
