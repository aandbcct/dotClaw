import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SearchDialog } from "./SearchDialog";
import { parseSearchSources } from "./SearchSources";
import { exportMarkdown } from "./preferences";
import { MessageContent } from "./MessageContent";
import { SettingsPanel } from "./SettingsPanel";
import type { RunActivity, RuntimeSettings } from "./types";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); });
const tool = { call_id: "c1", name: "builtin.web.search", status: "completed", arguments: {}, result: "", error: "", started_at: "", completed_at: "" };
const activity: RunActivity = { run_id: "r1", status: "completed", approval_id: null, child_run_id: null, tools: [] };

describe("阅读与管理", () => {
  it("搜索防抖废弃过期响应，清空查询不保留旧结果", async () => {
    let firstResolve: (value: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn((url: string) => String(url).includes("q=first") ? new Promise<Response>((resolve) => { firstResolve = resolve; }) : Promise.resolve(new Response(JSON.stringify({ items: [{ session_id: "s", title: "新结果", snippet: "<script>纯文本", message_id: "c2", archived: true }], has_more: false })))));
    const user = userEvent.setup(); render(<SearchDialog onClose={() => undefined} onSelect={() => undefined} />);
    const input = screen.getByLabelText("搜索历史正文");
    await user.type(input, "first");
    await new Promise((resolve) => setTimeout(resolve, 300));
    await user.clear(input); await user.type(input, "second");
    await screen.findByText("新结果");
    firstResolve(new Response(JSON.stringify({ items: [{ session_id: "old", title: "旧结果", snippet: "old", message_id: null }], has_more: false })));
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(screen.queryByText("旧结果")).toBeNull();
    expect(screen.getByText("<script>纯文本")).toBeTruthy();
    expect(document.querySelector("script")).toBeNull();
    await user.clear(input);
    await screen.findByText("输入关键词，搜索全部对话与归档记录");
    expect(screen.queryByText("新结果")).toBeNull();
  });

  it("搜索来源过滤危险链接、失败结果和非法 JSON，并按 URL 去重", () => {
    const result = JSON.stringify({ results: [
      { title: "正常", url: "https://example.org/page", snippet: "<b>文本</b>" },
      { title: "重复", url: "https://example.org/page" }, { title: "危险", url: "javascript:alert(1)" },
      { title: "", url: "https://example.org/empty" }, { title: "认证", url: "https://user:pass@example.org/" },
    ] });
    const sources = parseSearchSources([{ ...activity, tools: [{ ...tool, result }, { ...tool, result: "bad json" }, { ...tool, status: "failed", result: JSON.stringify({ results: [{ title: "失败", url: "https://bad.org" }] }) }] }]);
    expect(sources).toEqual([{ title: "正常", url: "https://example.org/page", snippet: "<b>文本</b>", domain: "example.org" }]);
    expect(parseSearchSources([{ ...activity, tools: [{ ...tool, result: '{"results":[]}' }] }])).toEqual([]);
  });

  it("导出原始 Markdown、清理文件名并释放下载 URL", async () => {
    let blob: Blob | undefined;
    const create = vi.fn((value: Blob) => { blob = value; return "blob:test"; });
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
    let download = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { download = this.download; });
    exportMarkdown({ id: "s1", title: "中文/计划", agent_id: "default", model: "m1", created_at: "", updated_at: "", messages: [{ id: "c1", role: "assistant", content: "表格\n\n```py\n  code\n```", created_at: "" }] }, true);
    expect(download).toBe("中文_计划.md");
    const content = await blob!.text();
    expect(content).toContain("  code");
    expect(content).toContain("仅包含已完成记录");
    expect(content).toContain("不代表每条旧回答");
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:test"));
  });

  it("行内和块级公式渲染，代码中的美元符号保持原文", async () => {
    const { container } = render(<MessageContent content={'行内 $x^2$\n\n$$\nE=mc^2\n$$\n\n```sh\necho "$HOME"\n```'} />);
    expect(container.querySelectorAll(".katex").length).toBe(2);
    expect(container.querySelector("pre")?.textContent).toBe('echo "$HOME"\n');
  });

  it("长代码可展开并复制完整原文，已有图片可以放大", async () => {
    const user = userEvent.setup(); const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const code = Array.from({ length: 30 }, (_, i) => "  line " + i).join("\n") + "\n";
    render(<MessageContent content={'```text\n' + code + '```\n\n![图片](https://example.org/image.png)'} />);
    await user.click(await screen.findByRole("button", { name: "展开完整代码" }));
    expect(screen.getByRole("button", { name: "收起代码" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "复制代码" }));
    expect(write).toHaveBeenCalledWith(code);
    await user.click(screen.getByRole("button", { name: "放大图片：图片" }));
    expect(screen.getByRole("dialog", { name: "图片预览" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "关闭图片预览" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("设置失败保留弹层，工具列表支持过滤与回调", async () => {
    const settings: RuntimeSettings = { general: { stream: true, max_context_tokens: 8000, keep_recent_messages: 10, exec_timeout: 60, scheduler_enabled: true }, models: [], tools: [{ name: "read", description: "读取文本", source: "builtin", enabled: true, needs_approval: false }, { name: "write", description: "写入文本", source: "builtin", enabled: false, needs_approval: true }], skills: [], mcp_enabled: true, mcp_servers: [] };
    const toggle = vi.fn(); const user = userEvent.setup();
    render(<SettingsPanel settings={settings} loading={false} busyKey={null} error="配置读取失败" onClose={() => undefined} onToggleTool={toggle} onToggleSkill={() => undefined} onReconnectMcp={() => undefined} />);
    expect(screen.getByRole("dialog", { name: "设置" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("配置读取失败");
    await user.click(screen.getByRole("button", { name: "工具" }));
    await user.type(screen.getByLabelText("过滤工具"), "read");
    expect(screen.queryByRole("checkbox", { name: "开启 write" })).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "关闭 read" }));
    expect(toggle).toHaveBeenCalledWith("read", false);
  });
});
