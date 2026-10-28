import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PACKAGE || "playwright");
const base = process.env.GUI_URL || "http://127.0.0.1:5173";
const output = path.resolve(process.cwd(), "../tmp/gui-evidence/final");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const a = { id: "visual-a", title: "梳理 Agent 的执行与恢复流程", agent_id: "default", model: "chat-model", created_at: "2026-10-23T00:00:00Z", updated_at: new Date().toISOString() };
const content = "## 从请求到结果\n\n每个会话独立保存运行状态。用户可以在等待期间继续阅读其他对话。\n\n```python\nasync def run(session_id):\n    async with lease(session_id):\n        return await execute()\n```\n\n| 阶段 | 行为 |\n|---|---|\n| 请求 | 校验会话 |\n| 执行 | 保存运行事实 |\n| 完成 | 更新历史 |\n\n失败时保留明确的错误信息，审批后继续原来的运行。";
const messages = [{ id: "c1", role: "user", content: "请解释执行流程，并给出代码和表格。", created_at: a.created_at }, { id: "c1:assistant", role: "assistant", content, created_at: a.created_at, run_ids: ["run-a", "run-child"] }];
const activity = { run_id: "run-a", status: "completed", approval_id: null, child_run_id: null, tools: [{ call_id: "tool-a", name: "builtin.web.search", status: "completed", started_at: "2026-10-23T00:00:01Z", completed_at: "2026-10-23T00:00:02Z", arguments: { query: "Agent execution" }, result: JSON.stringify({ query: "Agent execution", results: [{ title: "运行架构说明", url: "https://example.org/architecture", snippet: "会话、运行和工具执行的边界。" }] }), error: "" }] };
const models = Array.from({ length: 12 }, (_, i) => ({ name: i === 0 ? "chat-model" : "model-" + i, model_id: i === 0 ? "chat-model-v1" : "provider-model-" + i, provider: "example-provider", context_window: 128000, capabilities: i === 2 ? ["embedding"] : ["chat", "function_calling"], status: "active", reasoning_mode: "native", api_key_configured: true, preferred: i === 0 }));
const settings = { general: { stream: true, max_context_tokens: 8000, keep_recent_messages: 10, exec_timeout: 60, scheduler_enabled: true }, models, tools: Array.from({ length: 20 }, (_, i) => ({ name: "builtin.files.tool_" + i, description: "读取工作区中的文件；更多描述默认收起，权限策略继续生效。", source: "builtin", enabled: i % 3 !== 0, needs_approval: i % 2 === 0 })), skills: [{ name: "code-review", description: "审查代码", enabled: true, always_load: false, emoji: "" }], mcp_enabled: true, mcp_servers: [{ name: "demo", transport: "stdio", endpoint: "demo-server", state: "connected", detail: "", tool_count: 3 }] };
const options = { models: ["chat-model", "fast-model"], default_model: "chat-model", default_agent_id: "default", agents: [{ id: "default", name: "dotClaw", description: "通用助手" }] };
const results = [];
async function fixture(viewport, theme, scenario = "chat") {
  const context = await browser.newContext({ viewport });
  await context.addInitScript(({ theme }) => { localStorage.setItem("dotclaw.theme", theme); localStorage.setItem("dotclaw.sidebar.v1", "false"); }, { theme });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let sessions = scenario === "new" ? [] : Array.from({ length: 20 }, (_, i) => ({ ...a, id: i ? "visual-" + i : a.id, title: i ? "项目讨论 " + i : a.title }));
  let history = scenario === "reading" ? [{ ...messages[1], content: "行内公式 $x^2$ 与块级公式：\n\n$$\nE=mc^2\n$$\n\n```python\n" + Array.from({ length: 40 }, (_, i) => "    item_" + i + " = '用于验证长代码保留缩进与局部滚动'").join("\n") + "\n```\n\n| 长字段 | 结果 |\n|---|---|\n| " + "A".repeat(300) + " | 正常 |\n\n![测试图](http://127.0.0.1:5173/acceptance-image.svg)" }] : messages;
  let model = a.model;
  let sends = 0, creates = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const p = url.pathname;
    let data;
    if (p.endsWith("/sessions/search")) data = url.searchParams.get("q")?.includes("不存在") ? { items: [], has_more: false } : { items: [{ session_id: a.id, title: a.title, archived: false, updated_at: a.updated_at, message_id: history.findLast((message) => message.role === "assistant")?.id || null, role: "assistant", snippet: "每个会话独立保存运行状态。" }], has_more: false };
    else if (p.endsWith("/options")) data = options;
    else if (p.endsWith("/settings")) data = settings;
    else if (p.endsWith("/model")) {
      if (scenario === "model-error") return route.fulfill({ status: 409, json: { error: { code: "session_busy", message: "此会话正在其他页面执行，请结束后再切换模型。" } } });
      model = request.postDataJSON().model; data = { ...a, model };
    } else if (p.endsWith("/messages")) {
      sends++;
      const question = request.postDataJSON().content;
      history = [...history, { id: "c2", role: "user", content: question, created_at: a.created_at }, { id: "c2:assistant", role: "assistant", content: "已完成的夹具回答", created_at: a.created_at, run_ids: ["run-new"] }];
      return route.fulfill({ contentType: "text/event-stream", body: 'event: message.delta\ndata: {"kind":"response_delta","content":"已完成的夹具回答","run_id":"run-new"}\n\nevent: run.completed\ndata: {"run_id":"run-new","final_message":{"content":"已完成的夹具回答"}}\n\n' });
    } else if (request.method() === "DELETE") {
      sessions = sessions.filter((item) => !p.endsWith(item.id));
      return route.fulfill({ status: 204 });
    } else if (p.endsWith("/sessions")) {
      if (request.method() === "POST") { creates++; sessions = [a]; history = []; return route.fulfill({ status: 201, json: a }); }
      data = sessions;
    } else if (p.endsWith("/activity")) {
      const id = url.searchParams.get("run_id");
      if (id) data = { ...activity, run_id: id };
      else if (scenario === "approval") data = { ...activity, run_id: "run-live", status: "waiting_approval", approval_id: "approval-a", tools: [{ ...activity.tools[0], name: "builtin.files.write_text", status: "approval_required", result: "", arguments: { path: "demo.txt", content: "待审内容" } }] };
      else if (scenario === "generating") data = { ...activity, status: "running" };
      else data = null;
    } else if (request.method() === "PATCH") {
      const changes = request.postDataJSON(); sessions = sessions.map((item) => p.endsWith(item.id) ? { ...item, ...changes } : item); data = sessions.find((item) => p.endsWith(item.id)) || a;
    } else data = { ...a, model, messages: history };
    await route.fulfill({ json: data });
  });
  await page.route("**/acceptance-image.svg", (route) => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="240"><rect width="480" height="240" fill="#eaece9"/><text x="32" y="130" fill="#202422" font-size="28">Image preview fixture</text></svg>' }));
  await page.goto(base);
  await page.getByRole("textbox", { name: "输入消息" }).waitFor();
  await page.waitForTimeout(150);
  async function nav(label) {
    if (viewport.width < 768) await page.getByRole("button", { name: "打开会话列表" }).click();
    await page.getByRole("button", { name: label, exact: true }).click();
  }
  return { page, context, nav, errors, count: () => ({ creates, sends }) };
}
async function capture(f, file) {
  await f.page.screenshot({ path: path.join(output, file), animations: "disabled" });
  const metrics = await f.page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    dialogOverflow: [...document.querySelectorAll("dialog[open]")].some((el) => el.scrollWidth > el.clientWidth + 1),
    inputVisible: (() => { const r = document.querySelector("textarea").getBoundingClientRect(); return r.bottom <= innerHeight && r.top >= 0; })(),
  }));
  assert.equal(metrics.overflow, false, file + " 页面横向溢出");
  assert.equal(metrics.dialogOverflow, false, file + " 弹层横向溢出");
  assert.deepEqual(f.errors, [], file + " 页面异常");
  results.push({ file, ...metrics });
}
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }, { width: 390, height: 844 }]) for (const theme of ["light", "dark"]) {
    const suffix = viewport.width + "-" + theme + ".png";
    let f = await fixture(viewport, theme);
    await f.page.getByRole("region", { name: "对话内容" }).evaluate((el) => { el.scrollTop = 0; });
    await capture(f, "chat-" + suffix);
    await f.nav("设置");
    await capture(f, "settings-" + suffix);
    await f.page.getByRole("button", { name: "工具", exact: true }).click();
    await capture(f, "settings-tools-" + suffix);
    await f.page.keyboard.press("Escape");
    await f.page.getByRole("button", { name: "执行记录", exact: true }).click();
    await f.page.getByText("参数", { exact: true }).first().click();
    await capture(f, "execution-" + suffix);
    await f.page.keyboard.press("Escape");
    await f.nav("搜索对话");
    await f.page.getByRole("searchbox").fill("不存在");
    await f.page.getByText("没有找到匹配的对话").waitFor();
    await capture(f, "search-empty-" + suffix);
    await f.page.keyboard.press("Escape");
    await f.context.close();
    f = await fixture(viewport, theme, "new");
    await capture(f, "new-" + suffix);
    await f.context.close();
    f = await fixture(viewport, theme, "approval");
    await f.page.getByRole("group", { name: "工具审批" }).waitFor();
    await capture(f, "approval-" + suffix);
    await f.page.locator("article.message--assistant").last().getByText("参数", { exact: true }).click();
    await capture(f, "approval-detail-" + suffix);
    await f.context.close();
    f = await fixture(viewport, theme, "model-error");
    await f.page.getByRole("button", { name: /chat-model/ }).click();
    await f.page.getByRole("button", { name: "fast-model", exact: true }).click();
    await f.page.getByRole("alert").waitFor();
    await capture(f, "model-error-" + suffix);
    await f.context.close();
    f = await fixture(viewport, theme, "generating");
    await f.page.getByText("结果待确认", { exact: true }).waitFor();
    await capture(f, "generating-" + suffix);
    await f.context.close();
  }

  // 端到端验收使用浏览器内隔离接口夹具，绝不发送真实模型或删除用户数据。
  const f = await fixture({ width: 1440, height: 900 }, "light", "new");
  await f.page.getByRole("textbox", { name: "输入消息" }).fill("夹具首条问题");
  await f.page.getByRole("button", { name: "发送消息" }).click();
  await f.page.getByText("已完成的夹具回答").waitFor();
  assert.deepEqual(f.count(), { creates: 1, sends: 1 });
  await f.page.getByRole("button", { name: /chat-model/ }).click();
  await f.page.getByRole("button", { name: "fast-model", exact: true }).click();
  await f.page.getByRole("button", { name: /fast-model/ }).waitFor();
  await f.nav("搜索对话");
  await f.page.getByRole("searchbox").fill("会话");
  await f.page.getByRole("dialog", { name: "搜索对话" }).getByRole("button", { name: /梳理 Agent/ }).click();
  await f.page.locator(".message--highlight").waitFor();
  await f.page.getByRole("button", { name: "当前会话更多操作" }).click();
  const download = f.page.waitForEvent("download");
  await f.page.getByRole("button", { name: "导出 Markdown" }).click();
  assert.match((await download).suggestedFilename(), /\.md$/);
  await f.page.getByRole("button", { name: "当前会话更多操作" }).click();
  await f.page.getByRole("button", { name: "归档", exact: true }).click();
  await f.nav("已归档");
  await f.page.getByRole("button", { name: /更多操作：梳理/ }).click();
  await f.page.getByRole("button", { name: "恢复会话" }).click();
  await f.page.getByText("暂无归档会话").waitFor();
  await f.nav("返回最近会话");
  await f.page.getByRole("button", { name: /更多操作：梳理/ }).click();
  await f.page.getByRole("button", { name: "删除会话", exact: true }).click();
  assert.equal(await f.page.getByRole("button", { name: "取消", exact: true }).evaluate((el) => el === document.activeElement), true);
  await f.page.getByRole("button", { name: "确认删除" }).click();
  await f.page.getByText("暂无会话").waitFor();
  await f.context.close();

  const keyboard = await fixture({ width: 1280, height: 720 }, "dark");
  await keyboard.nav("设置");
  for (let i = 0; i < 30; i++) {
    await keyboard.page.keyboard.press("Tab");
    assert.equal(await keyboard.page.evaluate(() => Boolean(document.activeElement?.closest('dialog[open]'))), true);
  }
  await keyboard.page.keyboard.press("Escape");
  assert.equal(await keyboard.page.getByRole("button", { name: "设置", exact: true }).evaluate((el) => el === document.activeElement), true);
  await keyboard.context.close();
  const zoom = await fixture({ width: 720, height: 450 }, "light");
  await capture(zoom, "zoom-equivalent-200-percent.png");
  await zoom.context.close();
  for (const [viewport, theme] of [[{ width: 1440, height: 900 }, "light"], [{ width: 390, height: 844 }, "dark"]]) {
    const reading = await fixture(viewport, theme, "reading");
    assert.equal(await reading.page.locator(".katex").count(), 2);
    await reading.page.getByRole("button", { name: "展开完整代码" }).click();
    await reading.page.getByRole("button", { name: "收起代码" }).click();
    await reading.page.getByRole("region", { name: "对话内容" }).evaluate((el) => { el.scrollTop = 0; });
    const tableMetrics = await reading.page.locator(".table-scroll").evaluate((el) => ({ viewport:innerWidth, width:el.clientWidth, scrollWidth:el.scrollWidth, tableWidth:el.querySelector("table").getBoundingClientRect().width, textLength:el.querySelector("td").textContent.length, wrap:getComputedStyle(el.querySelector("td")).overflowWrap }));
    assert.ok(tableMetrics.scrollWidth > tableMetrics.width, JSON.stringify(tableMetrics));
    assert.equal(await reading.page.locator(".table-scroll").evaluate((el) => { const cells = el.querySelectorAll("tbody td"); return cells[0].getBoundingClientRect().right <= cells[1].getBoundingClientRect().left + 1; }), true);
    await capture(reading, "reading-" + viewport.width + "-" + theme + ".png");
    await reading.page.getByRole("button", { name: "放大图片：测试图" }).click();
    await capture(reading, "image-preview-" + viewport.width + "-" + theme + ".png");
    await reading.context.close();
  }
  function luminance(hex) {
    const c = hex.match(/\w\w/g).map((v) => parseInt(v, 16) / 255).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
    return c[0] * .2126 + c[1] * .7152 + c[2] * .0722;
  }
  const contrast = [];
  for (const [theme, surfaces, texts, button] of [
    ["light", ["ffffff", "f5f5f4", "eaece9"], ["202422", "606962", "356348", "b42318"], ["ffffff", "356348"]],
    ["dark", ["191a1b", "212325", "292c2f", "34383b"], ["ecefec", "abb4ad", "a7d3b0", "ffb4ab"], ["17291e", "a7d3b0"]],
  ]) {
    for (const [foreground, background] of [...texts.flatMap((color) => surfaces.map((surface) => [color, surface])), button]) {
      const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
      const ratio = (values[0] + .05) / (values[1] + .05);
      assert.ok(ratio >= 4.5, `${theme} ${foreground}/${background}: ${ratio}`);
      contrast.push({ theme, foreground, background, ratio: Number(ratio.toFixed(2)) });
    }
  }
  await writeFile(path.join(output, "contrast.json"), JSON.stringify(contrast, null, 2));
  await writeFile(path.join(output, "acceptance.json"), JSON.stringify({ capturedAt: new Date().toISOString(), results, workflow: "passed", keyboard: "passed", realProviderCalls: 0, limitations: ["手机软键盘与真实中文输入法候选窗需要真机验收", "200% 使用等效 CSS 视口，不代表真实浏览器缩放手势"] }, null, 2));
  console.log(JSON.stringify({ screenshots: results.length, output, workflow: "passed", keyboard: "passed" }));
} finally { await browser.close(); }
