import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { ToolApprovalSummary, ToolResultView } from "./ToolPresentation";
import { parseToolSearchSources } from "./SearchSources";
import type { ToolExecution } from "./types";

const tool: ToolExecution = { call_id: "c", name: "builtin.process.execute", arguments: { command: "echo 示例命令" }, result: "示例输出", error: "", status: "completed", started_at: "", completed_at: "" };

it("命令审批完整展示并复制，覆盖写入直接展示路径及可折叠内容", async () => {
  const user = userEvent.setup();
  const command = "echo 不执行的假命令 " + "x".repeat(4100);
  const { rerender } = render(<ToolApprovalSummary tool={{ ...tool, arguments: { command } }} />);
  expect(screen.getByText(command)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "复制命令" }));
  expect(await navigator.clipboard.readText()).toBe(command);
  rerender(<ToolApprovalSummary tool={{ ...tool, name: "builtin.files.write_text", arguments: { path: "demo.md", content: "<script>不会执行</script>" } }} />);
  expect(screen.getByText("写入文件（覆盖模式）")).toBeTruthy();
  expect(screen.getByText("demo.md")).toBeTruthy();
  expect(screen.getByText("待写入内容预览").closest("details")?.open).toBe(false);
  await user.click(screen.getByText("待写入内容预览"));
  expect(screen.getByText("<script>不会执行</script>")).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
});

it("长结果截断仅影响展示，展开和复制保留完整文本", async () => {
  const user = userEvent.setup(), result = "正文".repeat(2100) + "END";
  render(<ToolResultView tool={{ ...tool, result }} />);
  expect(screen.getByText(/已截断展示前 4000 字符/)).toBeTruthy();
  expect(document.querySelector("pre")?.textContent?.length).toBe(4000);
  await user.click(screen.getByRole("button", { name: "复制完整结果" }));
  expect(await navigator.clipboard.readText()).toBe(result);
  await user.click(screen.getByRole("button", { name: "展开全部" }));
  expect(document.querySelector("pre")?.textContent).toBe(result);
  await user.click(screen.getByRole("button", { name: "收起" }));
  expect(document.querySelector("pre")?.textContent?.length).toBe(4000);
});

it.each(["builtin.files.read_text", "builtin.files.list_directory", "builtin.files.write_text", "unknown.tool"])("%s 内容只作纯文本，保留原有结果，不猜测业务成功", (name) => {
  render(<ToolResultView tool={{ ...tool, name, arguments: { path: "demo" }, result: '<img src="x" onerror="alert(1)">\nERROR: 假错误文本' }} />);
  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector("pre")?.textContent).toContain("onerror");
  expect(screen.queryByText("原始结果", { exact: true })).toBeNull();
  expect(screen.queryByText(/退出码|执行成功/)).toBeNull();
});

it("搜索复用安全来源解析，保留原始结果；异常结果回退纯文本", () => {
  const search = { ...tool, name: "builtin.web.search", result: JSON.stringify({ results: [
    { title: "安全来源", url: "https://example.org/doc", snippet: "示例" },
    { title: "重复", url: "https://example.org/doc" },
    { title: "禁止", url: "javascript:alert(1)" },
    { title: "凭据", url: "https://user:password@example.org/" },
  ] }) };
  const { rerender } = render(<ToolResultView tool={search} />);
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.getByRole("link").getAttribute("rel")).toBe("noopener noreferrer");
  expect(screen.getByText("原始结果", { exact: true, selector: "summary" }).closest("details")?.open).toBe(false);
  expect(parseToolSearchSources({ ...search, status: "failed" })).toEqual([]);
  rerender(<ToolResultView tool={{ ...search, result: "不是 JSON 的原始内容" }} />);
  expect(screen.getByText("不是 JSON 的原始内容")).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});

it("审批参数类型不匹配回退原始参数，缺失工具不猜测", () => {
  const { rerender } = render(<ToolApprovalSummary tool={{ ...tool, arguments: { command: 123 } }} />);
  expect(screen.getByText(/"command": 123/)).toBeTruthy();
  rerender(<ToolApprovalSummary />);
  expect(screen.getByText("审批操作详情暂不可用，请刷新")).toBeTruthy();
});
