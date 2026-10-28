import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { RunActivityPanel } from "./RunActivityPanel";
import type { DisplayMessage, RunActivity } from "./types";

const message: DisplayMessage = { id: "m", role: "assistant", content: "", created_at: "", run_ids: ["r"], execution: { stage: "processing", error: null, statusError: "", submittedText: "原问题" } };
const activity: RunActivity = { run_id: "r", status: "running", approval_id: null, child_run_id: null, tools: [{ call_id: "c", name: "读取文件", status: "failed", started_at: "", completed_at: "", arguments: { path: "demo" }, result: "", error: "没有权限" }], steps: [{ id: "r:1", sequence: 1, kind: "model", status: "completed", started_at: "", completed_at: "", call_id: null }, { id: "r:3", sequence: 3, kind: "tool", status: "failed", started_at: "", completed_at: "", call_id: "c" }] };
const actions = () => ({ onLoadRun: vi.fn(), onCheckStatus: vi.fn(), onRestoreInput: vi.fn(), onApprove: vi.fn(), onResumeDelegation: vi.fn(), onResumeRun: vi.fn() });

it("无正文立即显示真实处理反馈，思考首次展开后尊重手动折叠，完成折叠不反复关闭", async () => {
  const user = userEvent.setup(), callbacks = actions();
  const { rerender } = render(<RunActivityPanel {...callbacks} message={message} activities={[activity]} canControl={false} />);
  expect(screen.getByText("处理中").closest("[role=status]")).toBeTruthy();
  expect(screen.queryByText("思考过程")).toBeNull();
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByText("没有权限")).toBeTruthy();
  const thinking = { ...message, reasoning: "真实返回内容", execution: { ...message.execution!, stage: "thinking" as const } };
  rerender(<RunActivityPanel {...callbacks} message={thinking} activities={[activity]} canControl={false} />);
  const details = screen.getByText("思考过程").closest("details")!;
  expect(details.open).toBe(true);
  await user.click(screen.getByText("思考过程"));
  rerender(<RunActivityPanel {...callbacks} message={{ ...thinking, reasoning: "真实返回内容继续" }} activities={[activity]} canControl={false} />);
  expect(details.open).toBe(false);
  const completed = { ...thinking, execution: { ...thinking.execution, stage: "completed" as const } };
  rerender(<RunActivityPanel {...callbacks} message={completed} activities={[activity]} canControl={false} />);
  expect(screen.queryByRole("list")).toBeNull();
  const toggle = screen.getByRole("button", { name: "执行记录" });
  toggle.focus(); await user.keyboard("{Enter}");
  rerender(<RunActivityPanel {...callbacks} message={completed} activities={[{ ...activity }]} canControl={false} />);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(callbacks.onLoadRun).toHaveBeenCalledWith("r");
});

it("多个运行独立分组，旧 tools 可降级；历史审批禁用，当前审批携带原运行 ID", async () => {
  const user = userEvent.setup(), callbacks = actions();
  const legacy = { ...activity, run_id: "child", status: "waiting_approval", steps: undefined, approval_id: "approval" };
  const { rerender } = render(<RunActivityPanel {...callbacks} message={{ ...message, run_ids: ["r", "child"] }} activities={[activity, legacy]} canControl={false} />);
  expect(within(screen.getByRole("region", { name: "运行过程 r" })).getAllByRole("listitem")).toHaveLength(2);
  expect(within(screen.getByRole("region", { name: "运行过程 child" })).getAllByRole("listitem")).toHaveLength(1);
  expect((screen.getByRole("button", { name: "批准并继续" }) as HTMLButtonElement).disabled).toBe(true);
  rerender(<RunActivityPanel {...callbacks} message={{ ...message, run_ids: ["child"] }} activities={[legacy]} canControl />);
  await user.click(screen.getByRole("button", { name: "批准并继续" }));
  expect(callbacks.onApprove).toHaveBeenCalledWith("child", true);
});
