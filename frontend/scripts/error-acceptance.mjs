import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PACKAGE || "playwright");
const output = path.resolve("../tmp/gui-p0-errors");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const a = { id: "a", title: "错误验收 A", agent_id: "default", model: "fake-model", created_at: "2026-10-28T00:00:00Z", updated_at: "2026-10-28T00:00:00Z" };
const b = { ...a, id: "b", title: "错误验收 B" };
const error = { code: "empty_model_response", message: "模型未返回有效回答，请查看执行记录或稍后重新发送。", retryable: false };
const event = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
const results = [];
async function fixture(mode, viewport = { width: 1440, height: 900 }, theme = "light") {
  const context = await browser.newContext({ viewport });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await context.addInitScript((theme) => localStorage.setItem("dotclaw.theme", theme), theme);
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  let sends = 0, started = false, queryFails = mode === "query-failure", completed = false, release;
  const queries = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname;
    // 所有业务请求均由隔离夹具响应，不接触用户会话、真实模型或外部服务。
    if (p.endsWith("/messages")) {
      sends++; started = true;
      const answer = (body) => route.fulfill({ contentType: "text/event-stream", body });
      if (p.includes("/b/")) return answer(event("run.cancelled", { run_id: "run-b" }));
      if (mode === "concurrent") return new Promise((resolve) => { release = async () => { await answer(event("run.failed", { run_id: "run-a", error })); resolve(); }; });
      if (mode === "rejected") return route.fulfill({ status: 422, json: { error: { code: "invalid_request", message: "请求参数被明确拒绝", retryable: false } } });
      if (mode === "network") return route.abort("connectionfailed");
      if (mode === "http500") return route.fulfill({ status: 502, contentType: "text/html", body: "bad gateway" });
      if (mode === "protocol") return route.fulfill({ json: { error: "不是 SSE" } });
      if (mode === "unknown-event") return answer(event("heartbeat", { run_id: "must-ignore" }));
      if (mode === "partial") return answer(event("message.delta", { run_id: "run-a", kind: "response_delta", content: "仍保留的部分正文" }));
      if (mode === "approval") return answer(event("run.suspended", { run_id: "run-a", status: "waiting_approval", approval_id: "approval-a" }));
      return answer(event("run.failed", { run_id: "run-a", error: mode === "recoverable" ? { ...error, code: "service_unavailable", retryable: true } : error }));
    }
    if (p.includes("/activity")) {
      queries.push(url.search);
      if (!started) return route.fulfill({ json: null });
      if (queryFails) return route.fulfill({ status: 503, json: { error: { code: "query_failed", message: "状态接口暂时不可用" } } });
      if (!url.searchParams.get("run_id") || (mode === "partial" && !completed)) return route.fulfill({ json: null });
      const status = mode === "recoverable" ? "recoverable" : mode === "approval" ? "waiting_approval" : mode === "partial" ? "completed" : "failed";
      return route.fulfill({ json: { run_id: "run-a", status, tools: status === "waiting_approval" ? [{ call_id: "approval-tool", name: "unknown", arguments: {}, result: "", error: "", status: "approval_required", started_at: "", completed_at: "" }] : [], approval_id: status === "waiting_approval" ? "approval-a" : null, approval_call_id: status === "waiting_approval" ? "approval-tool" : null, error: status === "failed" ? error : null } });
    }
    if (p.includes("/resume") || p.includes("/approvals/")) return route.fulfill({ contentType: "text/event-stream", body: event("run.cancelled", { run_id: "run-a" }) });
    if (p.endsWith("/sessions")) return route.fulfill({ json: [a, b] });
    const messages = mode === "historical-empty" ? [{ id: "old", role: "assistant", content: "", created_at: a.created_at }] : completed ? [{ id: "saved", role: "assistant", content: "确认后的最终正文", created_at: a.created_at, run_ids: ["run-a"] }] : [];
    return route.fulfill({ json: { ...(p.endsWith("/b") ? b : a), messages } });
  });
  await page.goto(process.env.GUI_URL || "http://127.0.0.1:5173");
  await page.getByRole("button", { name: "当前会话更多操作" }).waitFor();
  async function send() { await page.getByLabel("输入消息").fill("需要保留的原始问题"); await page.getByRole("button", { name: "发送消息" }).click(); }
  async function selectB() {
    if (viewport.width < 768) await page.getByRole("button", { name: "打开会话列表" }).click();
    await page.getByRole("button", { name: "错误验收 B", exact: true }).click();
  }
  return { context, page, pageErrors, send, selectB, count: () => sends, queries, release: () => release(), resolveQuery: () => { queryFails = false; }, complete: () => { completed = true; } };
}
async function capture(f, name) {
  await f.page.screenshot({ path: path.join(output, name + ".png"), animations: "disabled" });
  assert.deepEqual(f.pageErrors, []);
  assert.equal(await f.page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
  results.push(name);
}
try {
  for (const [viewport, theme] of [[{ width: 1440, height: 900 }, "light"], [{ width: 1440, height: 900 }, "dark"], [{ width: 390, height: 844 }, "light"]]) {
    for (const mode of ["empty", "network", "rejected"]) {
      const f = await fixture(mode, viewport, theme);
      await f.send();
      await f.page.getByRole("alert").waitFor();
      const alert = f.page.getByRole("alert");
      if (mode === "empty") {
        await alert.getByText(error.message).waitFor();
        await f.page.getByRole("button", { name: "查看执行记录", exact: true }).click();
        await f.page.getByRole("button", { name: "复制运行 ID", exact: true }).click();
        assert.equal(await f.page.evaluate(() => navigator.clipboard.readText()), "run-a");
        await f.page.getByLabel("输入消息").fill("已有草稿");
        await f.page.getByRole("button", { name: "恢复原问题到输入框" }).click();
        assert.equal(await f.page.getByLabel("输入消息").inputValue(), "已有草稿\n\n需要保留的原始问题");
      } else if (mode === "network") {
        assert.match(await alert.innerText(), /结果待确认/);
        assert.equal(await f.page.getByRole("button", { name: "发送消息" }).isDisabled(), true);
      } else await alert.getByText("请求参数被明确拒绝").waitFor();
      await capture(f, `${mode}-${viewport.width}-${theme}`);
      if (mode === "empty") {
        await f.page.getByRole("button", { name: "查看执行记录", exact: true }).click();
        await f.page.getByRole("region", { name: "运行过程 run-a" }).waitFor();
        await capture(f, `details-${viewport.width}-${theme}`);
      }
      assert.equal(f.count(), 1);
      await f.context.close();
    }
  }
  for (const mode of ["http500", "protocol", "unknown-event", "query-failure", "historical-empty", "recoverable", "approval", "partial", "concurrent"]) {
    const f = await fixture(mode);
    if (mode === "historical-empty") await f.page.getByText("该次回答为空", { exact: true }).waitFor();
    else {
      await f.send();
      if (mode === "concurrent") {
        await f.selectB();
        await f.page.getByLabel("输入消息").fill("B 可以独立发送");
        await f.page.getByRole("button", { name: "发送消息" }).click();
        await f.release();
        assert.equal(f.count(), 2);
        assert.equal(await f.page.getByText(error.message, { exact: true }).count(), 0);
      } else if (mode === "query-failure") {
        await f.page.getByRole("button", { name: "查询运行状态" }).click();
        await f.page.getByText(/状态查询失败/).waitFor();
        f.resolveQuery();
        await f.page.getByRole("button", { name: "查询运行状态" }).click();
        await f.page.getByText(/状态查询失败/).waitFor({ state: "hidden" });
        await f.page.getByText(error.message, { exact: true }).waitFor();
        assert.ok(f.queries.slice(1).every((query) => query.includes("run_id=run-a")));
      } else if (mode === "recoverable" || mode === "approval") {
        await f.page.getByRole("button", { name: mode === "approval" ? "批准并继续" : "恢复运行" }).click();
      } else {
        await f.page.getByRole("alert").waitFor();
        assert.match(await f.page.getByRole("alert").innerText(), /结果待确认/);
        if (mode === "partial") {
          await f.page.getByText("仍保留的部分正文", { exact: true }).waitFor();
          f.complete();
          await f.page.getByRole("button", { name: "查询运行状态" }).click();
          await f.page.getByText("确认后的最终正文", { exact: true }).waitFor();
          await f.page.getByRole("alert").waitFor({ state: "hidden" });
        }
      }
    }
    await capture(f, mode);
    if (mode !== "concurrent") assert.ok(f.count() <= 1);
    await f.context.close();
  }
  await writeFile(path.join(output, "acceptance.json"), JSON.stringify({ results, passed: true, realProviderCalls: 0, realSessionMutations: 0, note: "浏览器隔离 HTTP/SSE 夹具；真实上游空输出原因未验证" }, null, 2));
  console.log(JSON.stringify({ screenshots: results.length, passed: true, output }));
} finally { await browser.close(); }
