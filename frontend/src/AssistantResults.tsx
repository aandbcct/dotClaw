import type { AssistantOperation, AssistantRunResults, AssistantTaskRef } from "./types";

const taskStatus: Record<AssistantTaskRef["status"], string> = {
  pending: "待办", in_progress: "进行中", completed: "已完成", cancelled: "已取消",
};
const outcomeLabels: Record<AssistantOperation["result"]["outcome"], string> = {
  succeeded: "已成功", failed: "失败", partial: "部分成功",
};

/** 服务端快照按原顺序展示；定位始终使用真实事务ID。 */
export function AssistantResults({
  results,
  errors,
  loading,
  onOpenTask,
  onRetry,
}: {
  results: AssistantRunResults[];
  errors: Array<{ runId: string; message: string }>;
  loading: boolean;
  onOpenTask: (taskId: string) => void;
  onRetry: (runId: string) => void;
}) {
  const writes = results.flatMap((result) => result.operations
    .filter((operation) => ["tasks.create", "tasks.update", "tasks.status", "reminders.create", "reminders.update", "reminders.status", "batch.apply"].includes(operation.result.operation))
    .map((operation) => ({ runId: result.run_id, operation })));
  return <section className="assistant-results" aria-label="助理执行结果">
    <header><h2>事务结果</h2>{loading && <span role="status">正在同步…</span>}</header>
    {errors.map((error) => <p className="assistant-result-error" role="alert" key={error.runId}>
      {error.message}<button type="button" onClick={() => onRetry(error.runId)}>重试读取结果</button>
    </p>)}
    {results.flatMap((result) => result.result_sets.map((set) => <article className="assistant-result-card" key={set.id}>
      <header><strong>查询快照</strong><small>运行 {result.run_id}</small></header>
      {!set.items.length ? <p className="muted">这次查询没有匹配事务。</p> : <ol>
        {set.items.map((item) => <li key={item.id}>
          <button type="button" onClick={() => onOpenTask(item.id)}>{item.title}</button>
          <span>{taskStatus[item.status]} · v{item.version}</span>
          {item.deadline && <small>截止：{item.deadline}</small>}
          {item.reminder_summary && <small>{item.reminder_summary}</small>}
        </li>)}
      </ol>}
    </article>))}
    {writes.map(({ runId, operation }) => <OperationResult key={`${runId}:${operation.operation_key}`} runId={runId} operation={operation} onOpenTask={onOpenTask} />)}
    {!loading && !results.length && !errors.length && <p className="muted">完成事务查询或操作后，结果会显示在这里。</p>}
  </section>;
}

function OperationResult({ runId, operation, onOpenTask }: { runId: string; operation: AssistantOperation; onOpenTask: (taskId: string) => void }) {
  const isBatch = operation.result.operation === "batch.apply";
  const tasks = isBatch ? [] : operation.result.task_refs;
  const reminderTaskIds = isBatch ? [] : [...new Set(operation.result.reminder_refs.map((reminder) => reminder.task_id))];
  const taskIds = new Set(tasks.map((task) => task.id));
  const header = operation.result.outcome === "succeeded" ? "操作结果" : operation.result.outcome === "failed" ? "操作失败" : "操作部分完成";
  return <article className="assistant-result-card">
    <header><strong>{header}</strong><small>{outcomeLabels[operation.result.outcome]} · 运行 {runId}</small></header>
    {operation.result.error && <p className="assistant-operation-error" role="alert">{operation.result.error.message}</p>}
    {isBatch ? <ol className="assistant-batch-result-items">{operation.result.items.map((item) => <li key={item.index}>
      <strong>项目 {item.index}：{outcomeLabels[item.outcome]}</strong>
      {item.error && <p className="assistant-operation-error" role="alert">{item.error.message}</p>}
      {(item.task_refs.length > 0 || item.reminder_refs.length > 0) && <ul>
        {item.task_refs.map((task) => <li key={task.id}><button type="button" onClick={() => onOpenTask(task.id)}>{task.title}</button><span>{taskStatus[task.status]} · v{task.version}</span></li>)}
        {[...new Set(item.reminder_refs.map((reminder) => reminder.task_id))].filter((id) => !item.task_refs.some((task) => task.id === id)).map((id) => <li key={id}><button type="button" onClick={() => onOpenTask(id)}>打开提醒所属事务</button></li>)}
      </ul>}
    </li>)}</ol> : (tasks.length > 0 || reminderTaskIds.length > 0) && <ul>
      {tasks.map((task) => <li key={task.id}><button type="button" onClick={() => onOpenTask(task.id)}>{task.title}</button><span>{taskStatus[task.status]} · v{task.version}</span></li>)}
      {reminderTaskIds.filter((id) => !taskIds.has(id)).map((id) => <li key={id}><button type="button" onClick={() => onOpenTask(id)}>打开提醒所属事务</button></li>)}
    </ul>}
  </article>;
}
