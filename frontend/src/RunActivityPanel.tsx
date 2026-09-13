import type { RunActivity } from "./types";

interface RunActivityPanelProps {
  activity: RunActivity;
  busy: boolean;
  onApprove: (approved: boolean) => void;
  onResumeDelegation: () => void;
  onResumeRun: () => void;
}

const STATUS_LABELS: Record<string, string> = {
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  approval_required: "等待审批",
  cancelled: "已取消",
};

export function RunActivityPanel({
  activity,
  busy,
  onApprove,
  onResumeDelegation,
  onResumeRun,
}: RunActivityPanelProps) {
  const actionable = ["waiting_approval", "waiting_delegation", "recoverable"].includes(activity.status);
  if (activity.tools.length === 0 && !actionable) return null;

  return (
    <aside className="run-activity" aria-label="运行过程">
      <div className="activity-heading">
        <div><span className={`activity-dot activity-dot--${activity.status}`} /><strong>执行记录</strong></div>
        <code>{activity.run_id.slice(0, 8)}</code>
      </div>

      {activity.tools.map((tool) => (
        <details className="tool-step" key={tool.call_id}>
          <summary>
            <span className={`tool-status tool-status--${tool.status}`} aria-hidden="true" />
            <strong>{tool.name}</strong>
            <span>{STATUS_LABELS[tool.status] ?? tool.status}</span>
            <span className="tool-expand">展开</span>
          </summary>
          <div className="tool-detail">
            <section><h4>参数</h4><pre>{JSON.stringify(tool.arguments, null, 2)}</pre></section>
            {tool.result && <section><h4>结果</h4><pre>{tool.result}</pre></section>}
            {tool.error && <section className="tool-error"><h4>错误</h4><pre>{tool.error}</pre></section>}
          </div>
        </details>
      ))}

      {activity.status === "waiting_approval" && (
        <div className="approval-card" role="group" aria-label="工具审批">
          <div><strong>需要你的批准</strong><span>展开上方工具记录，确认参数后再决定。</span></div>
          <button type="button" className="secondary-button" disabled={busy} onClick={() => onApprove(false)}>拒绝</button>
          <button type="button" className="primary-button" disabled={busy} onClick={() => onApprove(true)}>批准并继续</button>
        </div>
      )}
      {activity.status === "waiting_delegation" && (
        <div className="approval-card">
          <div><strong>父任务正在等待子任务</strong><span>子任务完成后，可在这里继续父任务。</span></div>
          <button type="button" className="primary-button" disabled={busy} onClick={onResumeDelegation}>继续父任务</button>
        </div>
      )}
      {activity.status === "recoverable" && (
        <div className="approval-card">
          <div><strong>发现未结束的运行</strong><span>可以从已保存的检查点继续。</span></div>
          <button type="button" className="primary-button" disabled={busy} onClick={onResumeRun}>恢复运行</button>
        </div>
      )}
    </aside>
  );
}
