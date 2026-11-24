import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ChatWorkspace } from "./ChatWorkspace";
import type { AssistantRunResults, DisplayMessage } from "./types";

const makeSnapshot = (runId: string, title: string): AssistantRunResults => ({
  schema_version: 1, run_id: runId, operations: [], result_sets: [{ id: `${runId}-snapshot`, session_id: "personal-assistant", run_id: runId, call_id: `${runId}-query`, created_at: "", items: [{ id: `${runId}-item`, title, description: "查询内容", status: "pending", version: 1, deadline: null, planning: { kind: "task", event_time: null, scheduled_time: null, time_text: "", related_task_id: null } }] }],
});

const message = (id: string, runId: string, content: string): DisplayMessage => ({ id, role: "assistant", content, created_at: "", run_ids: [runId], execution: { stage: "completed", error: null, statusError: "", submittedText: "" } });

function renderWorkspace(results: AssistantRunResults[], options: { stage?: NonNullable<DisplayMessage["execution"]>["stage"]; assistantMode?: boolean; noExecution?: boolean } = {}) {
  const stage = options.stage || "completed";
  const running = ["recoverable", "waiting_approval", "waiting_delegation"].includes(stage);
  const messages = [message("m1", "r1", "第一次查询完成"), message("m2", "r2", "第二次查询完成")].map((item) => item.id === "m2" ? options.noExecution ? { ...item, execution: undefined } : { ...item, execution: { ...item.execution!, stage } } : item);
  const activitiesByRunId = { r1: { run_id: "r1", status: "completed", approval_id: null, child_run_id: null, tools: [] }, r2: { run_id: "r2", status: stage === "recoverable" ? "recoverable" : stage === "waiting_approval" ? "waiting_approval" : stage === "waiting_delegation" ? "waiting_delegation" : "completed", approval_id: stage === "waiting_approval" ? "approval-2" : null, child_run_id: null, tools: [] } };
  const view = { messages, activitiesByRunId, phase: running ? stage : "idle", activeMessageId: running ? "m2" : null, activity: null, error: "", loading: false, runId: running ? "r2" : null, abandoningRunId: null };
  const control = vi.fn();
  const workspace = { selected: { id: "personal-assistant", title: "个人助理", model: "test", agent_id: "default", created_at: "", updated_at: "", messages: [] }, selectedId: "personal-assistant", view, views: { "personal-assistant": view }, loading: false, isStreaming: () => false, control, abandon: vi.fn(), loadRunActivity: vi.fn(), checkRunStatus: vi.fn() } as never;
  const setDraft = vi.fn();
  const props = { workspace, assistantMode: options.assistantMode ?? true, active: true, ready: true, statusMessage: "", draft: "保留草稿", setDraft, storageFailed: false, theme: "light" as const, onToggleTheme: vi.fn(), onOpenNavigation: vi.fn(), results, resultErrors: [], resultsLoading: false, onRetryResults: vi.fn(), onFocusTask: vi.fn() };
  return { ...render(<ChatWorkspace {...props} />), setDraft, props, control };
}

it("两次查询不打扰主对话，详情手动打开、关闭后保持关闭并保留草稿", () => {
  const first = makeSnapshot("r1", "快照甲");
  const second = makeSnapshot("r2", "快照乙");
  const { rerender, props, setDraft } = renderWorkspace([first]);

  expect(screen.getByText("第一次查询完成")).toBeTruthy();
  expect(screen.getByText("第二次查询完成")).toBeTruthy();
  expect(screen.queryByText("快照甲")).toBeNull();
  expect(screen.queryByText("查询快照")).toBeNull();
  expect(screen.queryByRole("complementary", { name: "事务结果" })).toBeNull();
  expect(screen.getAllByRole("button", { name: "执行记录" })).toHaveLength(2);

  rerender(<ChatWorkspace {...props} results={[first, second]} />);
  expect(screen.queryByText("快照乙")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "事务结果" }));
  const aside = screen.getByRole("complementary", { name: "事务结果" });
  expect(within(aside).getByText("快照甲")).toBeTruthy();
  expect(within(aside).getByText("快照乙")).toBeTruthy();
  expect(within(aside).queryByRole("button", { name: "执行记录" })).toBeNull();
  expect(within(aside).queryByText("第一次查询完成")).toBeNull();
  fireEvent.click(within(aside).getByRole("button", { name: "快照甲" }));
  expect(props.onFocusTask).toHaveBeenCalledWith("r1-item");
  fireEvent.click(within(aside).getByRole("button", { name: "关闭" }));
  expect(screen.queryByText("快照甲")).toBeNull();
  expect(screen.queryByRole("complementary", { name: "事务结果" })).toBeNull();
  expect(setDraft).not.toHaveBeenCalled();
  rerender(<ChatWorkspace {...props} results={[first, second, makeSnapshot("r3", "关闭后新增快照")]}/>);
  expect(screen.queryByText("关闭后新增快照")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "事务结果" }));
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("complementary", { name: "事务结果" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "事务结果" }));
});

it("暂停状态可从对应回复的执行记录恢复；普通聊天维持原执行面板", () => {
  const { control } = renderWorkspace([], { stage: "recoverable", noExecution: true });
  expect(screen.queryByRole("complementary", { name: "事务结果" })).toBeNull();
  fireEvent.click(screen.getAllByRole("button", { name: "执行记录" }).at(-1)!);
  fireEvent.click(screen.getByRole("button", { name: "恢复运行" }));
  expect(control).toHaveBeenCalled();

  cleanup();
  renderWorkspace([makeSnapshot("r1", "普通聊天不应显示的快照")], { assistantMode: false });
  expect(screen.queryByRole("button", { name: "事务结果" })).toBeNull();
  expect(screen.queryByText("普通聊天不应显示的快照")).toBeNull();
  expect(screen.getAllByRole("button", { name: "执行记录" }).length).toBeGreaterThan(0);
});
