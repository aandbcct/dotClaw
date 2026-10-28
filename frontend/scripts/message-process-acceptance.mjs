import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PACKAGE || "playwright");
const output = path.resolve("../tmp/gui-message-process");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const session = { id: "inline-fixture", title: "消息内过程验收", agent_id: "default", model: "fake-model", created_at: "2026-10-28T00:00:00Z", updated_at: "2026-10-28T00:00:00Z" };
const results = [];
async function fixture(width = 1440, theme = "light", creating = false) {
  const context = await browser.newContext({ viewport: { width, height: width < 768 ? 844 : 900 }, reducedMotion: "reduce" });
  await context.addInitScript((theme) => {
    localStorage.setItem("dotclaw.theme", theme);
    const native = fetch.bind(window);
    const fixture = window.__process = { sends: 0, controls: 0 };
    window.fetch = (input, init) => {
      const url = String(input);
      if ((url.endsWith("/messages") || url.endsWith("/resume") || url.includes("/approvals/")) && init?.method === "POST") {
        if (url.endsWith("/messages")) fixture.sends++; else fixture.controls++;
        return Promise.resolve(new Response(new ReadableStream({ start(controller) { fixture.controller = controller; } }), { headers: { "Content-Type": "text/event-stream" } }));
      }
      return native(input, init);
    };
    fixture.push = (event, data) => fixture.controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
    fixture.close = () => fixture.controller.close();
  }, theme);
  const page = await context.newPage(), errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let activity = null, saved = false, queryFails = false, rejectCreation;
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    // 所有业务请求由隔离夹具处理，不访问真实会话、模型或配置。
    if (url.pathname.includes("/activity")) return queryFails ? route.fulfill({ status: 503, json: { error: { code: "query_failed", message: "隔离查询暂时不可用" } } }) : route.fulfill({ json: activity });
    if (url.pathname.endsWith("/options")) return route.fulfill({ json: { models: ["fake-model"], agents: [{ id: "default", name: "假模型助手", description: "" }], default_model: "fake-model", default_agent_id: "default" } });
    if (url.pathname.endsWith("/sessions")) {
      if (creating && route.request().method() === "POST") return new Promise((resolve) => { rejectCreation = () => route.fulfill({ status: 503, json: { error: { code: "fixture_creation_failed", message: "隔离创建失败" } } }).then(resolve); });
      return route.fulfill({ json: creating ? [] : [session] });
    }
    return route.fulfill({ json: { ...session, messages: saved ? [{ id: "saved", role: "user", content: "请读取示例文件并整理结果。", created_at: session.created_at }, { id: "saved:assistant", role: "assistant", content: "读取完成，以下是整理后的结果。", run_ids: ["inline-run"], created_at: session.created_at }] : [] } });
  });
  await page.goto(process.env.GUI_URL || "http://127.0.0.1:5173");
  if (creating) await page.getByText("有什么可以帮你？").waitFor();
  else await page.getByRole("button", { name: "当前会话更多操作" }).waitFor();
  const assistant = () => page.locator("article.message--assistant");
  const status = (text) => assistant().locator(".execution-status").filter({ hasText: text }).waitFor();
  const push = (event, data) => page.evaluate(([event, data]) => window.__process.push(event, data), [event, { session_id: session.id, run_id: "inline-run", ...data }]);
  const capture = async (name) => {
    const filename = `${name}-${width}-${theme}`;
    await page.screenshot({ path: path.join(output, filename + ".png"), animations: "disabled" });
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    assert.equal(await page.locator(".workspace-header [role=status]").count(), 0);
    assert.equal(await page.getByRole("dialog", { name: "执行详情" }).count(), 0);
    results.push(filename);
  };
  const send = async () => {
    await page.getByLabel("输入消息").fill("请读取示例文件并整理结果。");
    await page.getByRole("button", { name: "发送消息" }).click();
    await status("正在发送");
  };
  return { page, context, assistant, push, status, capture, send, activity: (value) => { activity = value; }, save: () => { saved = true; }, failQuery: () => { queryFails = true; }, rejectCreation: () => rejectCreation(), close: () => page.evaluate(() => window.__process.close()) };
}
const model = (sequence, status) => ({ id: `inline-run:${sequence}`, sequence, kind: "model", status, started_at: "2026-10-28T08:00:01Z", completed_at: status === "completed" ? "2026-10-28T08:00:02Z" : "", call_id: null });
const tool = (status) => ({ call_id: "read-1", name: "builtin.files.read_text", status, started_at: "2026-10-28T08:00:03Z", completed_at: status === "running" ? "" : "2026-10-28T08:00:04Z", arguments: { path: "demo.md" }, result: status === "completed" ? "隔离示例文件内容" : "", error: status === "failed" ? "示例文件读取失败" : "" });
const toolStep = (status) => ({ id: "inline-run:3", sequence: 3, kind: "tool", status, started_at: "2026-10-28T08:00:03Z", completed_at: "", call_id: "read-1" });
const activity = (status = "running", steps = [model(1, "running")], tools = [], event_sequence = 1) => ({ run_id: "inline-run", status, steps, tools, event_sequence, approval_id: status === "waiting_approval" ? "inline-approval" : null, child_run_id: null });
try {
  for (const [width, theme] of [[1440, "light"], [1440, "dark"], [390, "light"]]) {
    const f = await fixture(width, theme);
    await f.send(); await f.capture("sending");
    f.activity(activity()); await f.push("run.accepted", {}); await f.status("处理中"); await f.capture("processing");
    await f.push("message.delta", { kind: "reasoning_delta", content: "先读取文件，再根据实际内容整理。" }); await f.status("正在思考"); await f.capture("thinking");
    f.activity(activity("running", [model(1, "completed"), toolStep("running")], [tool("running")], 3));
    await f.status("正在执行工具"); await f.page.getByText("builtin.files.read_text", { exact: true }).waitFor();
    const parameters = f.assistant().getByText("参数", { exact: true }); await parameters.focus(); await f.page.keyboard.press("Enter");
    await f.capture("tool");
    f.activity(activity("running", [model(1, "completed"), toolStep("completed"), model(6, "running")], [tool("completed")], 6));
    await f.status("处理中");
    await f.push("message.delta", { kind: "response_delta", content: "读取完成，以下是整理后的结果。" }); await f.status("正在生成"); await f.capture("responding");
    f.save(); f.activity(activity("completed", [model(1, "completed"), toolStep("completed"), model(6, "completed")], [tool("completed")], 8));
    await f.push("run.completed", { final_message: { content: "读取完成，以下是整理后的结果。" } }); await f.close();
    await f.page.locator('[id="message-saved:assistant"]').waitFor(); await f.status("已完成"); await f.capture("completed");
    assert.equal(await f.page.locator("article.message").count(), 2);
    await f.assistant().getByRole("button", { name: "执行记录", exact: true }).click();
    await f.assistant().getByText("思考过程", { exact: true }).waitFor();
    assert.equal(await f.assistant().getByRole("listitem").count(), 3);
    await f.capture("completed-expanded");
    assert.equal(await f.page.evaluate(() => window.__process.sends), 1);
    await f.context.close();
  }
  for (const mode of ["approval", "failure", "disconnect"]) {
    const f = await fixture(mode === "approval" ? 390 : 1440);
    await f.send(); await f.push("run.accepted", {});
    await f.push("message.delta", { kind: "reasoning_delta", content: "这是模型真实返回的思考片段。" });
    const original = await f.assistant().getAttribute("id");
    if (mode === "approval") {
      f.activity(activity("waiting_approval", [model(1, "completed"), toolStep("approval_required")], [tool("approval_required")], 4));
      await f.push("run.suspended", { status: "waiting_approval", approval_id: "inline-approval" }); await f.close();
      await f.page.getByRole("button", { name: "批准并继续" }).waitFor(); await f.capture("approval");
      await f.page.getByRole("button", { name: "批准并继续" }).click(); await f.status("处理中"); await f.capture("approved-processing");
    } else if (mode === "failure") {
      const error = { code: "service_unavailable", message: "示例工具执行失败，请检查执行过程。", retryable: true };
      f.activity({ ...activity("failed", [model(1, "completed"), toolStep("failed")], [tool("failed")], 4), error });
      await f.push("run.failed", { error }); await f.close(); await f.page.getByRole("alert").filter({ hasText: error.message }).waitFor(); await f.capture("failure");
      await f.page.getByRole("button", { name: "查看执行记录", exact: true }).click();
      await f.page.getByText("示例文件读取失败", { exact: true }).waitFor(); await f.capture("failure-expanded");
      await f.page.getByRole("button", { name: "恢复原问题到输入框" }).click();
      assert.equal(await f.page.getByLabel("输入消息").inputValue(), "请读取示例文件并整理结果。");
      f.activity(activity("recoverable", [model(1, "completed"), toolStep("failed")], [tool("failed")], 4));
      await f.page.getByRole("button", { name: "查询运行状态" }).click();
      await f.page.getByRole("button", { name: "恢复运行" }).waitFor(); await f.capture("recoverable");
      await f.page.getByRole("button", { name: "恢复运行" }).click(); await f.status("处理中"); await f.capture("resumed-processing");
    } else {
      f.failQuery(); await f.close(); await f.status("结果待确认");
      await f.page.getByRole("button", { name: "查询运行状态" }).click(); await f.page.getByText(/状态查询失败/).waitFor(); await f.capture("disconnected");
    }
    assert.equal(await f.assistant().count(), 1);
    assert.equal(await f.assistant().getAttribute("id"), original);
    assert.equal(await f.page.evaluate(() => window.__process.sends), 1);
    if (mode !== "disconnect") {
      assert.equal(await f.page.evaluate(() => window.__process.controls), 1);
      await f.push("run.cancelled", {}); await f.close();
    }
    await f.context.close();
  }
  const initial = await fixture(390, "light", true);
  await initial.send(); await initial.capture("first-creation");
  await initial.rejectCreation(); await initial.page.getByRole("alert").filter({ hasText: "隔离创建失败" }).waitFor();
  assert.equal(await initial.page.getByLabel("输入消息").inputValue(), "请读取示例文件并整理结果。");
  assert.equal(await initial.page.evaluate(() => window.__process.sends), 0);
  await initial.capture("first-creation-failed"); await initial.context.close();
  await writeFile(path.join(output, "acceptance.json"), JSON.stringify({ passed: true, screenshots: results, realModelCalls: 0, realSessionMutations: 0, note: "浏览器可控 SSE 与活动投影夹具，不代表供应商验收" }, null, 2));
  console.log(JSON.stringify({ passed: true, screenshots: results.length, output }));
} finally { await browser.close(); }
