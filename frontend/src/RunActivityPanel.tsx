import { useEffect, useRef, useState, type ReactNode } from "react";
import type { DisplayMessage, RunActivity, ToolExecution } from "./types";
import { CopyButton } from "./MessageContent";
import { ToolApprovalSummary, ToolResultView } from "./ToolPresentation";

interface RunActivityPanelProps {
  message: DisplayMessage; activities: RunActivity[]; canControl: boolean;
  onLoadRun: (runId: string) => void; onCheckStatus: (runId?: string) => void;
  onRestoreInput: () => void; onApprove: (runId: string, approved: boolean) => void;
  onResumeDelegation: (runId: string) => void; onResumeRun: (runId: string) => void;
  onAbandon: (runId: string) => void; abandoningRunId: string | null;
  children?: ReactNode;
}
const LABELS: Record<string, string> = {
  sending: "正在发送", processing: "处理中", thinking: "正在思考", responding: "正在生成", tool_running: "正在执行工具", stopping: "正在停止",
  completed: "已完成", failed: "运行异常", cancelled: "已停止", unknown: "结果待确认", running: "执行中", approval_required: "等待审批",
  waiting_approval: "等待审批", waiting_delegation: "等待子任务", recoverable: "可恢复", interrupted: "已中断", abandoned: "已放弃",
};
const activeStages = new Set(["sending", "processing", "thinking", "responding", "tool_running", "stopping", "waiting_approval", "waiting_delegation", "recoverable", "unknown"]);

/** 思考与步骤分别展示真实来源；正文和操作仍属于同一条助手消息。 */
export function RunActivityPanel({ message, activities, canControl, onLoadRun, onCheckStatus, onRestoreInput, onApprove, onResumeDelegation, onResumeRun, onAbandon, abandoningRunId, children }: RunActivityPanelProps) {
  const execution = message.execution;
  const active = Boolean(execution && activeStages.has(execution.stage));
  const [expanded, setExpanded] = useState(active);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [confirmingRunId, setConfirmingRunId] = useState<string | null>(null);
  const reasoningSeen = useRef(false);
  const wasActive = useRef(active);
  useEffect(() => {
    if (active !== wasActive.current) { setExpanded(active); wasActive.current = active; }
  }, [active]);
  useEffect(() => {
    if (message.reasoning && !reasoningSeen.current) { setReasoningOpen(true); reasoningSeen.current = true; }
  }, [message.reasoning]);
  useEffect(() => {
    if (confirmingRunId && (!canControl || activities.find((activity) => activity.run_id === confirmingRunId)?.status !== "recoverable")) setConfirmingRunId(null);
  }, [activities, canControl, confirmingRunId]);
  function toggle(): void {
    const open = !expanded; setExpanded(open);
    if (open) for (const id of message.run_ids || []) onLoadRun(id);
  }
  const abandoned = execution?.stage === "abandoned" || (!execution && activities.find((activity) => activity.run_id === message.run_ids?.[0])?.status === "abandoned");
  const error = abandoned ? null : execution?.error || activities.find((activity) => activity.status !== "abandoned" && activity.error)?.error;
  return <div className="message-execution">
    <div className="execution-status" role="status">
      {active && !["unknown", "recoverable", "waiting_approval", "waiting_delegation"].includes(execution!.stage) ? <span className="execution-spinner" aria-hidden="true" /> : <span aria-hidden="true">{abandoned ? "⊘" : error ? "!" : active ? "◌" : "✓"}</span>}
      <span>{abandoned ? "已放弃" : execution ? LABELS[execution.stage] : "执行过程"}</span>
      <button type="button" aria-expanded={expanded} onClick={toggle}>{expanded ? "收起过程" : "执行记录"}</button>
    </div>
    {expanded && <div className="execution-process">
      {message.reasoning && <details className="reasoning" open={reasoningOpen} onToggle={(event) => setReasoningOpen(event.currentTarget.open)}><summary>思考过程</summary><p>{message.reasoning}</p></details>}
      {(message.run_ids || []).map((runId) => {
        const activity = activities.find((item) => item.run_id === runId);
        const controllable = canControl && !abandoningRunId && runId === message.run_ids?.[0] && activity && ["waiting_approval", "waiting_delegation", "recoverable"].includes(activity.status);
        const approvalTool = activity?.approval_call_id ? activity.tools.find((tool) => tool.call_id === activity.approval_call_id) : undefined;
        return <section className="run-activity" aria-label={`运行过程 ${runId}`} key={runId}>
          {!activity ? <p className="muted">执行记录尚未读取。<button type="button" onClick={() => onLoadRun(runId)}>读取执行记录</button></p> : <>
            <ol className="execution-steps">
              {activity.steps ? activity.steps.map((step) => <li key={step.id} className={`execution-step execution-step--${step.status}`}>
                {step.kind === "model" ? <><div className="tool-step-heading"><strong>模型调用</strong><span>{LABELS[step.status]}</span></div><StepTime start={step.started_at} end={step.completed_at} /></> : <ToolStep tool={activity.tools.find((tool) => tool.call_id === step.call_id)} status={step.status} />}
              </li>) : activity.tools.map((tool) => <li className={`execution-step execution-step--${tool.status}`} key={tool.call_id}><ToolStep tool={tool} /></li>)}
            </ol>
            {activity.status === "waiting_approval" && <div className="approval-card" role="group" aria-label="工具审批"><div><strong>需要你的批准</strong><ToolApprovalSummary tool={approvalTool} /></div><button type="button" disabled={!controllable} onClick={() => onApprove(runId, false)}>拒绝</button><button type="button" disabled={!controllable || !approvalTool} onClick={() => onApprove(runId, true)}>批准并继续</button>{!approvalTool && <button type="button" onClick={() => onCheckStatus(runId)}>重新查询审批</button>}</div>}
            {activity.status === "waiting_delegation" && <div className="approval-card"><span>父任务正在等待子任务</span><button type="button" disabled={!controllable} onClick={() => onResumeDelegation(runId)}>继续父任务</button></div>}
            {activity.status === "recoverable" && <div className="approval-card"><span>可从已保存的检查点恢复；不会重放已丢失的逐字增量。</span><button type="button" disabled={!controllable || confirmingRunId === runId} onClick={() => onResumeRun(runId)}>恢复运行</button>
              {confirmingRunId === runId ? <div className="abandon-confirm" role="group" aria-label="确认放弃运行"><p>放弃后不能恢复；已执行的操作不会撤销。</p><button type="button" onClick={() => setConfirmingRunId(null)}>取消放弃</button><button type="button" disabled={!controllable} onClick={() => { setConfirmingRunId(null); onAbandon(runId); }}>确认放弃</button></div> : <button type="button" disabled={!controllable} onClick={() => setConfirmingRunId(runId)}>{abandoningRunId === runId ? "放弃中…" : "放弃本次运行"}</button>}
            </div>}
            {activity.status === "abandoned" && <p className="muted">运行已放弃，检查点已清理；已执行的操作不会撤销。</p>}
          </>}
          <footer className="run-id"><span>运行 ID：{runId}</span><CopyButton label="复制运行 ID" getText={() => runId} /></footer>
        </section>;
      })}
    </div>}
    {children}
    {(error || execution?.statusError || execution?.stage === "unknown") && <div className={`execution-feedback ${execution?.stage === "unknown" ? "execution-feedback--unknown" : ""}`} role="alert">
      {error && <p>{error.message}</p>}
      {execution?.stage === "unknown" && !error && <p>结果待确认，请查询运行状态，不要重复发送。</p>}
      {execution?.statusError && <p>{execution.statusError}</p>}
      <div className="error-actions"><button type="button" onClick={() => { setExpanded(true); for (const id of message.run_ids || []) onLoadRun(id); }}>查看执行记录</button>{execution?.submittedText && <button type="button" onClick={onRestoreInput}>恢复原问题到输入框</button>}<button type="button" onClick={() => onCheckStatus(message.run_ids?.[0])}>查询运行状态</button></div>
    </div>}
  </div>;
}
function StepTime({ start, end }: { start: string; end: string }) {
  return start || end ? <p className="tool-time" title={[start, end].filter(Boolean).join(" → ")}>{start ? formatTime(start) : "未记录开始时间"}{end ? ` → ${formatTime(end)}` : ""}</p> : null;
}
function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function ToolStep({ tool, status }: { tool?: ToolExecution; status?: string }) {
  const value = status || tool?.status || "";
  return <><div className="tool-step-heading"><strong>{tool?.name || "工具调用"}</strong><span>{value === "completed" ? "调用结束" : LABELS[value] || value}</span></div>{tool && <><StepTime start={tool.started_at} end={tool.completed_at} />{tool.error && <p className="inline-error">{tool.error}</p>}<details><summary>参数</summary><pre>{JSON.stringify(tool.arguments, null, 2)}</pre></details><ToolResultView tool={tool} /></>}</>;
}
