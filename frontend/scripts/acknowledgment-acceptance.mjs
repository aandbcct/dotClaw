import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PACKAGE || "playwright");
const output = path.resolve("../tmp/gui-p1-acknowledgment");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
const session = { id: "p1-fixture", title: "接收确认验收", agent_id: "default", model: "fake-model", created_at: "2026-10-28T00:00:00Z", updated_at: "2026-10-28T00:00:00Z" };
try {
  for (const [viewport, theme] of [[{ width: 1440, height: 900 }, "light"], [{ width: 1440, height: 900 }, "dark"], [{ width: 390, height: 844 }, "light"]]) {
    for (const outcome of ["disconnected", "failed"]) {
    const context = await browser.newContext({ viewport });
    // 可控流只验证浏览器展示；真实 HTTP 首包时序由后端临时服务测试证明。
    await context.addInitScript((theme) => {
      localStorage.setItem("dotclaw.theme", theme);
      const nativeFetch = window.fetch.bind(window);
      const fixture = window.__ackFixture = { sends: 0 };
      window.fetch = (input, init) => {
        if (String(input).endsWith("/messages") && init?.method === "POST") {
          fixture.sends++;
          return new Promise((resolve) => {
            fixture.accept = () => resolve(new Response(new ReadableStream({ start(controller) { fixture.controller = controller; } }), { headers: { "Content-Type": "text/event-stream" } }));
          });
        }
        return nativeFetch(input, init);
      };
      fixture.push = (event, data) => fixture.controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      fixture.close = () => fixture.controller.close();
    }, theme);
    const page = await context.newPage();
    const errors = [], queries = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", (route) => {
      const url = new URL(route.request().url());
      // 所有业务读取均隔离，禁止访问真实会话或外部模型。
      if (url.pathname.includes("/activity")) {
        queries.push(url.search);
        return url.searchParams.has("run_id") ? route.fulfill({ status: 503, json: { error: { code: "fixture_query_failed", message: "隔离状态查询暂时不可用" } } }) : route.fulfill({ json: null });
      }
      if (url.pathname.endsWith("/sessions")) return route.fulfill({ json: [session] });
      return route.fulfill({ json: { ...session, messages: [] } });
    });
    const capture = async (stage) => {
      const name = `${stage}-${outcome}-${viewport.width}-${theme}`;
      await page.screenshot({ path: path.join(output, `${name}.png`), animations: "disabled" });
      assert.deepEqual(errors, []);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      results.push(name);
    };
    const push = (event, data) => page.evaluate(([event, data]) => window.__ackFixture.push(event, data), [event, data]);
    await page.goto(process.env.GUI_URL || "http://127.0.0.1:5173");
    await page.getByRole("button", { name: "当前会话更多操作" }).waitFor();
    await page.getByLabel("输入消息").fill("请说明本次运行的处理结果。");
    await page.getByRole("button", { name: "发送消息" }).click();
    await page.getByRole("status").filter({ hasText: "正在发送" }).waitFor();
    await capture("sending");
    await page.evaluate(() => window.__ackFixture.accept());
    await push("run.accepted", { session_id: session.id, run_id: "p1-confirmed-run" });
    await page.getByRole("status").filter({ hasText: "已接收，正在处理" }).waitFor();
    await page.getByText("运行 ID：p1-confirmed-run", { exact: true }).waitFor();
    await capture("accepted");
    await push("message.delta", { session_id: session.id, run_id: "p1-confirmed-run", kind: "response_delta", content: "这是隔离夹具提供的部分正文。" });
    await page.getByText("这是隔离夹具提供的部分正文。", { exact: true }).waitFor();
    await push("run.accepted", { session_id: session.id, run_id: "p1-confirmed-run" });
    assert.equal(await page.getByText("已接收，正在处理", { exact: true }).count(), 0);
    await capture("streaming");
    if (outcome === "failed") {
      await push("run.failed", { run_id: "p1-confirmed-run", error: { code: "empty_model_response", message: "模型未返回有效回答，请查看执行记录或稍后重新发送。", retryable: false } });
      await page.evaluate(() => window.__ackFixture.close());
      await page.getByRole("alert").filter({ hasText: "模型未返回有效回答" }).waitFor();
      await page.getByRole("button", { name: "恢复原问题到输入框" }).click();
      assert.equal(await page.getByLabel("输入消息").inputValue(), "请说明本次运行的处理结果。");
    } else {
      await page.evaluate(() => window.__ackFixture.close());
      await page.getByRole("alert").filter({ hasText: "结果待确认" }).waitFor();
      await page.getByRole("button", { name: "查询运行状态" }).click();
      await page.getByText(/状态查询失败/).waitFor();
      assert.ok(queries.some((query) => query.includes("run_id=p1-confirmed-run")));
    }
    assert.equal(await page.evaluate(() => window.__ackFixture.sends), 1);
    await capture(outcome);
    await context.close();
    }
  }
  await writeFile(path.join(output, "acceptance.json"), JSON.stringify({ passed: true, results, realProviderCalls: 0, realSessionMutations: 0, note: "浏览器可控流验证；真实 HTTP 首包由 test_sse_contract.py 验证" }, null, 2));
  console.log(JSON.stringify({ passed: true, screenshots: results.length, output }));
} finally { await browser.close(); }
