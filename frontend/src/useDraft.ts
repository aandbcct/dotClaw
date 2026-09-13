import { useEffect, useState } from "react";

const DRAFTS_KEY = "dotclaw.drafts";

function readDrafts(): Record<string, string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(DRAFTS_KEY) || "{}");
    if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter((entry) => typeof entry[1] === "string"));
  } catch {
    return {};
  }
}

/** 草稿按会话隔离，切换会话时不通过异步加载覆盖当前输入。 */
export function useDraft(sessionId: string | undefined) {
  const [drafts, setDrafts] = useState(readDrafts);
  const [storageFailed, setStorageFailed] = useState(false);
  useEffect(() => {
    try {
      localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
      setStorageFailed(false);
    } catch {
      setStorageFailed(true);
    }
  }, [drafts]);

  function setDraft(value: string): void {
    if (!sessionId) return;
    setDrafts((current) => {
      const next = { ...current };
      if (value) next[sessionId] = value;
      else delete next[sessionId];
      return next;
    });
  }

  return { draft: sessionId ? drafts[sessionId] || "" : "", setDraft, storageFailed };
}
