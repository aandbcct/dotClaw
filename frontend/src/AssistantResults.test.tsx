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
        { index: 1, outcome: "succeeded", task_refs: [{ id: "task-1", title: "准备报告", status: "pending", version: 1 }], reminder_refs: [], error: null },
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
