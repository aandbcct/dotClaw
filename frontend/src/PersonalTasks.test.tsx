import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PersonalTasks } from "./PersonalTasks";

afterEach(() => { vi.unstubAllGlobals(); });
const current = { id: "t1", title: "原题", description: "", status: "pending", deadline: null, created_at: "2026-11-18T00:00:00Z", updated_at: "2026-11-18T00:00:00Z", completed_at: null, version: 1, is_overdue: false };
const payload = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const list = (items: unknown[]) => ({ items, timezone: "Asia/Shanghai", as_of: "2026-11-18T00:00:00Z" });

describe("个人事务视图", () => {
  it("冲突后先读取，再由用户确认替换草稿与版本", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "PUT") return payload({ error: { code: "personal_task_version_conflict", message: "事务已被修改" } }, 409);
      if (url.endsWith("/t1")) return payload({ ...current, title: "服务器标题", version: 2 });
      return payload(list([current]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "编辑" }));
    const title = screen.getByLabelText("标题"); await user.clear(title); await user.type(title, "我的草稿");
    await user.click(screen.getByRole("button", { name: "保存事务" }));
    await user.click(await screen.findByRole("button", { name: "重新读取" }));
    expect(await screen.findByText(/确认后会替换当前草稿/)).toBeTruthy();
    expect((title as HTMLInputElement).value).toBe("我的草稿");
    await user.click(screen.getByRole("button", { name: "确认替换草稿" }));
    expect((title as HTMLInputElement).value).toBe("服务器标题");
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/t1"))).toBe(true);
  });

  it("提交期间禁用编辑并只发送一次创建请求", async () => {
    let finishPost: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishPost = resolve; });
      return Promise.resolve(payload(list([])));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup(); render(<PersonalTasks onOpenNavigation={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "新建" }));
    const title = screen.getByLabelText("标题"); await user.type(title, "新事务");
    fireEvent.click(screen.getByRole("button", { name: "保存事务" }));
    fireEvent.click(screen.getByRole("button", { name: "保存事务" }));
    await waitFor(() => expect((title as HTMLInputElement).disabled).toBe(true));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    finishPost?.(payload({ ...current, title: "新事务" }, 201));
    await waitFor(() => expect(screen.queryByLabelText("标题")).toBeNull());
  });
});
