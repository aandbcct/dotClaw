import type { SessionDetail } from "./types";

/** 浏览器偏好不可用时保持当前页面可操作。 */
export function readPreference<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) || "null") as T ?? fallback; } catch { return fallback; }
}

export function writePreference(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 本机偏好失败不阻止会话操作。 */ }
}

/** 只导出已完成正文，不包含工具参数或运行事实。 */
export function exportMarkdown(detail: SessionDetail, running: boolean): void {
  const text = `# ${detail.title}\n\n导出时间：${new Date().toISOString()}\n\n当前会话模型：${detail.model}（不代表每条旧回答的模型）\n\n${running ? '> 仅包含已完成记录\n\n' : ''}${detail.messages.map((message) => `## ${message.role === 'user' ? '用户' : '助手'}\n\n${message.content}`).join('\n\n---\n\n')}\n`;
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${detail.title.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/[. ]+$/, "").slice(0, 100) || "对话"}.md`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
