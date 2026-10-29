import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CopyButton, MessageContent } from "./MessageContent";

describe("消息展示与复制", () => {
  it("渲染表格和任务列表，高亮代码并复制带缩进的原始代码", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const code = 'const answer = 42;\n  console.log(answer);\n';
    const content = '| 名称 | 值 |\n| --- | --- |\n| 答案 | 42 |\n\n- [x] 已完成\n\n```js\n' + code + '```';
    const { container, rerender } = render(<MessageContent content={content} />);
    expect(screen.getByRole("table")).toBeTruthy();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    expect(container.querySelector(".hljs-keyword")?.textContent).toBe("const");
    await user.click(screen.getByRole("button", { name: "复制代码" }));
    expect(write).toHaveBeenCalledWith(code);
    expect(screen.getByText("已复制")).toBeTruthy();
    const tableScroll = container.querySelector<HTMLElement>(".table-scroll")!;
    tableScroll.scrollLeft = 120;
    rerender(<MessageContent content={content + "\n\n继续补充说明"} />);
    expect(container.querySelector(".table-scroll")?.scrollLeft).toBe(120);
  });

  it("复制回答保留 Markdown 原文，并显示剪贴板失败", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    render(<CopyButton label="复制回答" getText={() => "**原文**"} />);
    await user.click(screen.getByRole("button", { name: "复制回答" }));
    expect(write).toHaveBeenCalledWith("**原文**");
    expect(await screen.findByText("复制失败，请手动选择")).toBeTruthy();
  });

  it("未知代码语言正常显示，消息里的 HTML 不会执行", () => {
    const { container } = render(<MessageContent content={'```unknown-language\n<hello>\n```\n\n<script>alert(1)</script>'} />);
    expect(container.querySelector("pre")?.textContent).toBe("<hello>\n");
    expect(container.querySelector("script")).toBeNull();
  });
});
