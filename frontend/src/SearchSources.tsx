import { useEffect, useState } from "react";
import { getRunActivity } from "./api";
import type { RunActivity, ToolExecution } from "./types";

export interface SearchSource { title: string; url: string; snippet: string; domain: string }

/** 仅投影成功搜索工具的已知结果格式，外部内容一律按纯文本处理。 */
export function parseToolSearchSources(tool: ToolExecution): SearchSource[] {
  const seen = new Set<string>();
  const sources: SearchSource[] = [];
  if (tool.name !== "builtin.web.search" || tool.status !== "completed" || tool.error) return [];
  try {
    const data: unknown = JSON.parse(tool.result);
    if (!data || typeof data !== "object" || !("results" in data) || !Array.isArray(data.results)) return [];
    for (const item of data.results) {
      if (!item || typeof item !== "object" || typeof item.title !== "string" || !item.title.trim() || typeof item.url !== "string") continue;
      const url = new URL(item.url);
      if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || seen.has(url.href)) continue;
      seen.add(url.href);
      sources.push({ title: item.title, url: url.href, snippet: typeof item.snippet === "string" ? item.snippet : "", domain: url.hostname });
    }
  } catch { /* 非结构化结果保留在消息执行过程，不影响正文。 */ }
  return sources;
}

/** 单次工具和整轮来源汇总共用解析与链接校验。 */
export function parseSearchSources(activities: RunActivity[]): SearchSource[] {
  const seen = new Set<string>();
  return activities.flatMap((activity) => activity.tools.flatMap(parseToolSearchSources)).filter((source) => {
    if (seen.has(source.url)) return false;
    seen.add(source.url); return true;
  });
}
export function SearchSourceList({ sources }: { sources: SearchSource[] }) {
  return <div className="search-sources-list">
    {sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer"><strong>{source.title}</strong><span>{source.domain}</span>{source.snippet && <p>{source.snippet}</p>}</a>)}
    <p className="muted">来自已执行的搜索；来源列表不代表答案已经逐句验证。</p>
  </div>;
}

export function SearchSources({ sessionId, runIds }: { sessionId: string; runIds: string[] }) {
  const [activities, setActivities] = useState<RunActivity[]>([]);
  const [error, setError] = useState(false);
  const key = runIds.join("|");
  useEffect(() => {
    let active = true;
    setActivities([]); setError(false);
    void Promise.all(runIds.map((id) => getRunActivity(sessionId, id))).then((items) => {
      if (active) setActivities(items.filter((item): item is RunActivity => item !== null));
    }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [sessionId, key]);
  const sources = parseSearchSources(activities);
  if (!sources.length) return error ? <p className="muted">搜索记录读取失败，可在消息执行过程中重试查看。</p> : null;
  return <details className="search-sources"><summary>搜索结果 · {sources.length} 条</summary>
    <SearchSourceList sources={sources} />
  </details>;
}
