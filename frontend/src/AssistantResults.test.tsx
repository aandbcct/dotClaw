import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AssistantResults } from "./AssistantResults";
import type { AssistantRunResults } from "./types";

const results: AssistantRunResults = {
  schema_version: 1,
  run_id: "run-1",
  result_sets: [],
  operations: [
    { operation_key: "failed", session_id: "personal-assistant", run_id: "run-1", call_id: "c1", tool_name: "tasks.create", arguments_hash: "", request_id: null, created_at: "",
      result: { schema_version: 1, operation: "tasks.create", outcome: "failed", data: null, task_refs: [], reminder_refs: [], changes: [], error: { code: "conflict", message: "事务已变化" }, result_set_id: null, request_id: null, items: [] } },
    { operation_key: "partial", session_id: "personal-assistant", run_id: "run-1", call_id: "c2", tool_name: "batch.apply", arguments_hash: "", request_id: "request-1", created_at: "",
      result: { schema_version: 1, operation: "batch.apply", outcome: "partial", data: null, task_refs: [], reminder_refs: [], changes: [], error: null, result_set_id: null, request_id: "request-1", items: [
        { index: 1, outcome: "succeeded", task_refs: [{ id: "task-1", title: "准备报告", description: "", status: "pending", version: 1, deadline: null, planning: { kind: "task", event_time: null, scheduled_time: null, time_text: "", related_task_id: null } }], reminder_refs: [], error: null },
        { index: 2, outcome: "failed", task_refs: [], reminder_refs: [], error: { code: "stale", message: "目标事务已更新" } },
      ] } },
  ],
};

it("显示失败和部分成功卡片及可定位的已成功事务", () => {
  const onOpenTask = vi.fn();
  render(<AssistantResults results={[results]} errors={[]} loading={false} onRetry={vi.fn()} onOpenTask={onOpenTask} />);
  expect(screen.getByText("操作失败")).toBeTruthy();
  expect(screen.getByText("事务已变化")).toBeTruthy();
  expect(screen.getByText("操作部分完成")).toBeTruthy();
  expect(screen.getByText("项目 1：已成功")).toBeTruthy();
  expect(screen.getByText("项目 2：失败")).toBeTruthy();
  expect(screen.getByText("目标事务已更新")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "准备报告" }));
  expect(onOpenTask).toHaveBeenCalledExactlyOnceWith("task-1");
});

it("显示 plans.apply 创建的正式事务并支持定位", () => {
  const applied: AssistantRunResults = {
    schema_version: 1, run_id: "run-apply", result_sets: [], operations: [{
      operation_key: "apply-1", session_id: "personal-assistant", run_id: "run-apply", call_id: "call-apply", tool_name: "plans.apply", arguments_hash: "", request_id: "request-1", created_at: "",
      result: { schema_version: 1, operation: "plans.apply", outcome: "succeeded", data: null, task_refs: [{ id: "created-task", title: "复习线代", description: "第三章", status: "pending", version: 1, deadline: null, planning: { kind: "task", event_time: null, scheduled_time: { kind: "date", value: "2026-12-01" }, time_text: "下周", related_task_id: null } }], reminder_refs: [], changes: [], error: null, result_set_id: null, request_id: "request-1", items: [] },
    }],
  };
  const onOpenTask = vi.fn();
  render(<AssistantResults results={[applied]} errors={[]} loading={false} onRetry={vi.fn()} onOpenTask={onOpenTask} />);
  fireEvent.click(screen.getByRole("button", { name: "复习线代" }));
  expect(screen.getByText("操作结果")).toBeTruthy();
  expect(screen.getByText("待办 · v1")).toBeTruthy();
  expect(onOpenTask).toHaveBeenCalledExactlyOnceWith("created-task");
});

it("按查询快照的截止字符串展示旧格式字段", () => {
  const snapshot: AssistantRunResults = {
    schema_version: 1, run_id: "run-snapshot", operations: [], result_sets: [{
      id: "snapshot-1", session_id: "personal-assistant", run_id: "run-snapshot", call_id: "query-call", created_at: "",
      items: [{ id: "snapshot-task", title: "英语测验", description: "单词表", status: "pending", version: 2, deadline: "2026-12-02 09:00", planning: { kind: "event", event_time: { kind: "datetime", value: "2026-12-02T01:00:00Z" }, scheduled_time: null, time_text: "周三上午", related_task_id: null } }],
    }],
  };
  render(<AssistantResults results={[snapshot]} errors={[]} loading={false} onRetry={vi.fn()} onOpenTask={vi.fn()} />);
  expect(screen.getByText("截止：2026-12-02 09:00")).toBeTruthy();
});
