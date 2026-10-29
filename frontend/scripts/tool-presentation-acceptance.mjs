import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PACKAGE || "playwright");
const output = path.resolve("../tmp/gui-tool-presentation");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
const session = { id: "tool-fixture", title: "工具与放弃验收", agent_id: "default", model: "fake-model", created_at: "2026-10-28T00:00:00Z", updated_at: "2026-10-28T00:00:00Z" };
const tool = (call_id, name, args, result = "") => ({ call_id, name, arguments: args, result, status: "completed", started_at: "2026-10-28T08:00:00Z", completed_at: "2026-10-28T08:00:01Z", error: "" });
const command = tool("cmd", "builtin.process.execute", { command: "echo 当前审批的演示命令（此处不会执行）" });
const file = tool("write", "builtin.files.write_text", { path: "workspace/demo.md", content: "# 待写入预览\n这是隔离夹具中的内容，不会写入真实文件。\n" + "示例内容\n".repeat(900) });
const longResult = "stdout 示例，仅为已有输出文本\n" + "输出行\n".repeat(1200) + "完整结果结尾";
const sampleTools = [tool("cmd", "builtin.process.execute", command.arguments, longResult), tool("read", "builtin.files.read_text", { path: "workspace/demo.html" }, '<img src="x" onerror="alert(1)">\n文件内容按纯文本展示。'), tool("list", "builtin.files.list_directory", { path: "workspace" }, "demo.html\nnotes.md\nsubdirectory/"), tool("write", "builtin.files.write_text", file.arguments, "工具返回的写入说明（不代表提供 diff）"), tool("search", "builtin.web.search", { query: "示例" }, JSON.stringify({ results: [{ title: "隔离来源示例", url: "https://example.org/source", snippet: "已返回的工具来源，不代表答案已验证。" }] }))];
async function fixture(mode, width, theme) {
  const context = await browser.newContext({ viewport: { width, height: width < 768 ? 844 : 1000 }, reducedMotion: "reduce" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await context.addInitScript((theme) => localStorage.setItem("dotclaw.theme", theme), theme);
  const page = await context.newPage(), errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let posts = 0, sends = 0, release, queryFails = false;
  const isApproval = ["command", "write", "missing"].includes(mode);
  const tools = isApproval ? [tool("old", "builtin.process.execute", { command: "旧审批命令，不应作为本次摘要" }), mode === "write" ? file : command] : sampleTools;
  let activity = { run_id: "tool-run", status: isApproval ? "waiting_approval" : "recoverable", approval_id: isApproval ? "current-approval" : null, approval_call_id: isApproval ? mode === "missing" ? null : mode === "write" ? "write" : "cmd" : null, child_run_id: null, event_sequence: 10, tools, steps: tools.map((item, index) => ({ id: `tool-run:${index + 1}`, sequence: index + 1, kind: "tool", status: isApproval && item.call_id !== "old" ? "approval_required" : "completed", started_at: item.started_at, completed_at: item.completed_at, call_id: item.call_id })) };
  await page.route("**/api/v1/**", (route) => {
    const request = route.request(), url = new URL(request.url());
    // 所有业务请求均隔离；不执行命令、不写文件、不连接模型或外部搜索。
    if (url.pathname.endsWith("/abandon")) {
      posts++;
      if (mode === "network") { queryFails = true; return route.abort("connectionfailed"); }
      return new Promise((resolve) => { release = async () => { activity = { ...activity, status: "abandoned", event_sequence: 11, error: { code: "cancelled", message: "用户放弃运行" } }; await route.fulfill({ json: activity }); resolve(); }; });
    }
    if (url.pathname.endsWith("/messages")) { sends++; return route.fulfill({ contentType: "text/event-stream", body: 'event: run.cancelled\ndata: {"run_id":"new-run"}\n\n' }); }
    if (url.pathname.includes("/activity")) return queryFails ? route.fulfill({ status: 503, json: { error: { code: "query_failed", message: "隔离查询失败" } } }) : route.fulfill({ json: activity });
    if (url.pathname.endsWith("/sessions")) return route.fulfill({ json: [session] });
    return route.fulfill({ json: { ...session, messages: [{ id: "saved-user", role: "user", content: "请处理隔离示例任务。", created_at: session.created_at }, { id: "saved-assistant", role: "assistant", content: "已有的部分回答保留在这里。", run_ids: ["tool-run"], created_at: session.created_at }] } });
  });
  await page.goto(process.env.GUI_URL || "http://127.0.0.1:5173");
  await page.locator(".execution-status").filter({ hasText: isApproval ? "等待审批" : "可恢复" }).waitFor();
  const capture = async (stage) => {
    const name = `${stage}-${width}-${theme}`;
    await page.screenshot({ path: path.join(output, name + ".png"), animations: "disabled" });
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    results.push(name);
  };
  return { page, context, capture, counts: () => ({ posts, sends }), release: () => release() };
}
try {
  for (const [width, theme] of [[1440, "light"], [1440, "dark"], [390, "light"]]) {
    for (const mode of ["command", "write", "missing"]) {
      const f = await fixture(mode, width, theme), card = f.page.getByRole("group", { name: "工具审批" });
      await card.scrollIntoViewIfNeeded();
      if (mode === "command") {
        await card.getByText(command.arguments.command, { exact: true }).waitFor();
        assert.equal(await card.getByText("旧审批命令，不应作为本次摘要", { exact: true }).count(), 0);
        await f.capture(`approval-${mode}`);
        await card.getByRole("button", { name: "复制命令" }).click();
        assert.equal(await f.page.evaluate(() => navigator.clipboard.readText()), command.arguments.command);
      } else if (mode === "write") {
        await card.getByText("写入文件（覆盖模式）").waitFor();
        await card.getByText("待写入内容预览").click();
        await card.getByText(/已截断展示前 4000 字符/).waitFor();
        await card.evaluate((element) => element.scrollIntoView({ block: "start" }));
        await f.capture(`approval-${mode}`);
        await card.getByRole("button", { name: "复制完整结果" }).click();
        assert.equal(await f.page.evaluate(async () => (await navigator.clipboard.readText()).replace(/\r\n/g, "\n")), file.arguments.content);
      } else {
        assert.equal(await card.getByRole("button", { name: "批准并继续" }).isDisabled(), true);
        assert.equal(await card.getByRole("button", { name: "拒绝" }).isEnabled(), true);
      }
      if (mode === "missing") await f.capture(`approval-${mode}`);
      await f.context.close();
    }
    const f = await fixture("abandon", width, theme);
    const firstOutput = f.page.locator(".tool-output").first();
    await firstOutput.getByText(/已截断展示前 4000 字符/).waitFor();
    await firstOutput.getByRole("button", { name: "复制完整结果" }).click();
    assert.equal(await f.page.evaluate(async () => (await navigator.clipboard.readText()).replace(/\r\n/g, "\n")), longResult);
    assert.equal(await f.page.locator(".tool-output img").count(), 0);
    await firstOutput.scrollIntoViewIfNeeded(); await f.capture("command-output");
    await f.page.getByText("文件内容 · workspace/demo.html", { exact: true }).scrollIntoViewIfNeeded(); await f.capture("file-results");
    await f.page.getByRole("link", { name: /隔离来源示例/ }).first().scrollIntoViewIfNeeded(); await f.capture("search-results");
    await f.page.getByRole("button", { name: "放弃本次运行" }).click();
    await f.page.getByRole("group", { name: "确认放弃运行" }).scrollIntoViewIfNeeded(); await f.capture("abandon-confirm");
    await f.page.getByRole("button", { name: "取消放弃" }).click(); assert.equal(f.counts().posts, 0);
    await f.page.getByRole("button", { name: "放弃本次运行" }).click(); await f.page.getByRole("button", { name: "确认放弃" }).click();
    await f.page.getByRole("button", { name: "放弃中…" }).waitFor();
    assert.equal(await f.page.getByRole("button", { name: "恢复运行" }).isDisabled(), true);
    await f.capture("abandon-pending");
    await f.release(); await f.page.locator(".execution-status").filter({ hasText: "已放弃" }).waitFor();
    assert.equal(await f.page.getByRole("button", { name: "恢复运行" }).count(), 0);
    assert.equal(await f.page.getByRole("alert").count(), 0);
    await f.page.getByRole("button", { name: "执行记录", exact: true }).click();
    await f.page.getByText("运行已放弃，检查点已清理；已执行的操作不会撤销。", { exact: true }).waitFor();
    await f.page.locator(".execution-status").scrollIntoViewIfNeeded(); await f.capture("abandoned");
    await f.page.getByLabel("输入消息").fill("放弃后可以发送新问题"); await f.page.getByRole("button", { name: "发送消息" }).click();
    await f.page.getByText("放弃后可以发送新问题", { exact: true }).waitFor();
    assert.equal(await f.page.locator("article.message--assistant").count(), 2);
    assert.deepEqual(f.counts(), { posts: 1, sends: 1 });
    await f.context.close();
  }
  const f = await fixture("network", 390, "light");
  await f.page.getByRole("button", { name: "放弃本次运行" }).click(); await f.page.getByRole("button", { name: "确认放弃" }).click();
  await f.page.getByText(/放弃结果待确认/).waitFor();
  assert.deepEqual(f.counts(), { posts: 1, sends: 0 });
  await f.page.getByRole("alert").scrollIntoViewIfNeeded(); await f.capture("abandon-unknown"); await f.context.close();
  await writeFile(path.join(output, "acceptance.json"), JSON.stringify({ passed: true, screenshots: results, realModelCalls: 0, realCommandExecutions: 0, realFileWrites: 0, note: "隔离接口夹具，未调用真实工具或修改用户会话" }, null, 2));
  console.log(JSON.stringify({ passed: true, screenshots: results.length, output }));
} finally { await browser.close(); }
