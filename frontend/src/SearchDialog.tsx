import { useEffect, useState } from "react";
import { searchSessions } from "./api";
import { Dialog } from "./Dialog";
import type { SearchHit, SearchResults } from "./types";

/** 防抖与取消共同隔离过期搜索响应，原文只作为文本呈现。 */
export function SearchDialog({ onClose, onSelect }: { onClose: () => void; onSelect: (hit: SearchHit) => void }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResults | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setResult(null); setError(""); setLoading(Boolean(query.trim()));
    const timer = window.setTimeout(async () => {
      if (!query.trim()) return;
      try {
        const value = await searchSessions(query.trim(), controller.signal);
        if (active) setResult(value);
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "搜索失败");
      } finally { if (active) setLoading(false); }
    }, 250);
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [query]);
  return <Dialog title="搜索对话" className="search-dialog" onClose={onClose}>
    <input data-initial-focus autoFocus type="search" maxLength={100} aria-label="搜索历史正文" placeholder="搜索标题、问题和回答…" value={query} onChange={(event) => setQuery(event.target.value)} />
    <div className="search-results" aria-live="polite">
      {!query.trim() && <p className="muted empty-note">输入关键词，搜索全部对话与归档记录</p>}
      {loading && <p className="muted empty-note">正在搜索…</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {result?.items.length === 0 && <p className="muted empty-note">没有找到匹配的对话</p>}
      {result?.items.map((hit, index) => <button type="button" key={`${hit.session_id}-${hit.message_id}-${index}`} onClick={() => onSelect(hit)}><strong>{hit.title}</strong>{hit.archived && <span className="tag">已归档</span>}<p>{hit.snippet}</p></button>)}
      {result?.has_more && <p className="muted">还有更多结果，请缩小关键词范围。</p>}
    </div>
  </Dialog>;
}
