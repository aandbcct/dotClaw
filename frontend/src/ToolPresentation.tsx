import { useState } from "react";
import { CopyButton } from "./MessageContent";
import { parseToolSearchSources, SearchSourceList } from "./SearchSources";
import type { ToolExecution } from "./types";

const PREVIEW_LIMIT = 4000;

/** 只裁剪当前展示，不修改结果；复制始终使用完整原文。 */
function TextPreview({ text, label }: { text: string; label: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > PREVIEW_LIMIT;
  return <div className="tool-output"><div className="tool-output-heading"><strong>{label}</strong><CopyButton label="复制完整结果" getText={() => text} /></div>
    <pre>{long && !expanded ? text.slice(0, PREVIEW_LIMIT) : text}</pre>
    {long && <div className="tool-preview-actions"><span>{expanded ? `共 ${text.length} 字符` : `已截断展示前 ${PREVIEW_LIMIT} 字符，共 ${text.length} 字符`}</span><button type="button" onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "展开全部"}</button></div>}
  </div>;
}
function argument(tool: ToolExecution, key: string): string | null {
  const value = tool.arguments?.[key];
  return typeof value === "string" ? value : null;
}

/** 只展示当前审批精确关联的参数，不猜测其他工具的操作。 */
export function ToolApprovalSummary({ tool }: { tool?: ToolExecution }) {
  if (!tool) return <p>审批操作详情暂不可用，请刷新</p>;
  const command = argument(tool, "command"), path = argument(tool, "path"), content = argument(tool, "content");
  if (tool.name === "builtin.process.execute" && command !== null) return <div className="approval-summary"><strong>执行命令</strong><pre>{command}</pre><CopyButton label="复制命令" getText={() => command} /></div>;
  if (tool.name === "builtin.files.write_text" && path !== null && content !== null) return <div className="approval-summary"><strong>写入文件（覆盖模式）</strong><p>{path}</p><details><summary>待写入内容预览</summary><TextPreview text={content} label="待写入内容" /></details></div>;
  return <div className="approval-summary"><strong>{tool.name}</strong><pre>{JSON.stringify(tool.arguments, null, 2)}</pre></div>;
}

/** 使用后端状态和已有结果分类展示，文本不会被当作 HTML 执行。 */
export function ToolResultView({ tool }: { tool: ToolExecution }) {
  if (!tool.result) return null;
  const path = argument(tool, "path");
  if (tool.name === "builtin.web.search") {
    const sources = parseToolSearchSources(tool);
    if (sources.length) return <div className="tool-output search-sources"><SearchSourceList sources={sources} /><details><summary>原始结果</summary><TextPreview text={tool.result} label="原始结果" /></details></div>;
  }
  const label = tool.name === "builtin.process.execute" ? "命令输出" : tool.name === "builtin.files.read_text" ? `文件内容${path !== null ? ` · ${path}` : ""}` : tool.name === "builtin.files.list_directory" ? `目录内容${path !== null ? ` · ${path}` : ""}` : tool.name === "builtin.files.write_text" ? "写入调用结果" : "工具结果";
  return <TextPreview text={tool.result} label={label} />;
}
