import type { AssistantProposalItem } from "./types";
import type { AssistantPreviewState } from "./useAssistantResults";

const labels: Record<string, string> = {
  title: "标题", description: "描述", status: "状态", version: "版本", deadline: "截止时间",
  reminder_summary: "提醒", reminder_rule: "提醒规则",
};
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
function targetLabel(item: AssistantProposalItem): string {
  const target = item.target;
  return "task_id" in target ? `提醒 · ${target.id}` : `${target.title} · ${target.id}`;
}

/** 只展示服务端保存的前后快照；没有匹配快照时批准保持禁用。 */
export function AssistantBatchPreview({ state, onRetry }: { state: AssistantPreviewState | null; onRetry: () => void }) {
  if (!state || state.status === "loading") return <div className="assistant-batch-preview" role="status">正在读取已保存的批量差异…</div>;
  if (state.status !== "loaded" || !state.preview) return <div className="assistant-batch-preview" role="alert">
    <p>{state.error || "批准预览不可用"}</p><button type="button" onClick={onRetry}>重试加载预览</button>
  </div>;
  return <div className="assistant-batch-preview">
    <p>已核对提案 v{state.preview.version}，请确认以下变更：</p>
    <ol>{state.preview.items.map((item, index) => {
      const keys = [...new Set([...Object.keys(item.before), ...Object.keys(item.after)])];
      return <li key={`${item.target.id}:${index}`}>
        <strong>{targetLabel(item)}</strong>
        <dl>{keys.map((key) => <div key={key}>
          <dt>{labels[key] || key}</dt>
          <dd><del>{displayValue(item.before[key as keyof typeof item.before])}</del><span aria-hidden="true"> → </span><ins>{displayValue(item.after[key as keyof typeof item.after])}</ins></dd>
        </div>)}</dl>
      </li>;
    })}</ol>
  </div>;
}
