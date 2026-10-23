import type { RunActivity } from "./types";
import { CopyButton } from "./MessageContent";

interface RunActivityPanelProps {
  activity: RunActivity; busy: boolean; onApprove: (approved: boolean) => void;
  onResumeDelegation: () => void; onResumeRun: () => void;
}
const STATUS_LABELS: Record<string, string> = {
  running: "执行中", completed: "已完成", failed: "失败", approval_required: "等待审批",
  waiting_approval: "等待审批", waiting_delegation: "等待子任务", recoverable: "可恢复",
  cancelled: "已取消", abandoned: "已放弃",
};

/** 执行事实按步骤渐进披露；只有真实当前运行能执行控制操作。 */
export function RunActivityPanel({ activity, busy, onApprove, onResumeDelegation, onResumeRun }: RunActivityPanelProps) {
  return <aside className="run-activity" aria-label="运行过程">
    <div className="activity-heading"><strong>{STATUS_LABELS[activity.status] || activity.status}</strong><span className="muted">{activity.tools.length} 个工具</span></div>
    {!activity.tools.length && <p className="muted">此运行没有工具调用记录。</p>}
    {activity.tools.map((tool, index) => <section className="tool-step" key={tool.call_id}>
      <div className="tool-step-heading"><span className={`tool-status tool-status--${tool.status}`} aria-hidden="true" /><strong>{index + 1}. {tool.name}</strong><span className="muted">{STATUS_LABELS[tool.status] || tool.status}</span></div>
      <p className="tool-time">{formatTime(tool.started_at)}{tool.completed_at ? ` → ${formatTime(tool.completed_at)}` : ""}</p>
      {tool.error && <p className="inline-error">{tool.error}</p>}
      <details><summary>参数</summary><pre>{JSON.stringify(tool.arguments, null, 2)}</pre></details>
      {tool.result && <details><summary>原始结果</summary><pre>{tool.result}</pre></details>}
    </section>)}
    {activity.status === "waiting_approval" && <div className="approval-card" role="group" aria-label="工具审批">
      <div><strong>需要你的批准</strong><span>展开工具参数确认后再决定。</span></div>
      <button type="button" className="secondary-button" disabled={busy} onClick={() => onApprove(false)}>拒绝</button>
      <button type="button" className="primary-button" disabled={busy} onClick={() => onApprove(true)}>批准并继续</button>
    </div>}
    {activity.status === "waiting_delegation" && <div className="approval-card"><div><strong>父任务正在等待子任务</strong><span>子任务完成后可继续父任务。</span></div><button className="primary-button" type="button" disabled={busy} onClick={onResumeDelegation}>继续父任务</button></div>}
    {activity.status === "recoverable" && <div className="approval-card"><div><strong>发现未结束的运行</strong><span>从已保存的检查点恢复，不会重放已丢失的逐字增量。</span></div><button className="primary-button" type="button" disabled={busy} onClick={onResumeRun}>恢复运行</button></div>}
    <footer className="run-id"><span>运行 ID</span><code>{activity.run_id}</code><CopyButton label="复制运行 ID" getText={() => activity.run_id} /></footer>
  </aside>;
}
function formatTime(value: string): string {
  if (!value) return "未记录时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
