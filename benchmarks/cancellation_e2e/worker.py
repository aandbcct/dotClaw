"""真实浏览器取消基准；从命令行指定的隔离版本导入全部生产代码。"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback


def pid_alive(pid):
    """使用只读系统句柄确认已记录进程仍存活。"""
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def classify_failure(clicked, barrier_ready, missing_stop_entry):
    """归属与初始化失败不能冒充产品缺少停止入口。"""
    if clicked:
        return "failure"
    if barrier_ready and missing_stop_entry:
        return "entry_unavailable"
    return "invalid_fixture"


async def main(args):
    """装配原版应用并逐条记录，清理只能在样本事实冻结之后进行。"""
    source = Path(args.source).resolve()
    sys.path[:0] = [str(source / "src"), str(source)]
    import dotclaw
    import uvicorn
    from fastapi import Request
    from fastapi.responses import StreamingResponse
    from playwright.async_api import async_playwright
    from dotclaw.agent.identity import AgentIdentity
    from dotclaw.bootstrap.application_host import ApplicationHost
    from dotclaw.bootstrap.runtime_factory import build_runtime_services
    from dotclaw.bootstrap.session_interaction import SessionInteractionService
    from dotclaw.channel.web.app import create_app
    from dotclaw.config.settings import Config, load_router_config
    from dotclaw.llm.model_router import ModelRouter
    from dotclaw.llm.proxy import LLMProxy
    from dotclaw.llm.rate_limiter import RateLimiter
    from dotclaw.llm.circuit_breaker import CircuitBreaker
    from dotclaw.orchestration.registry import AgentRegistry
    from dotclaw.session.session import SessionManager
    from dotclaw.tools.discovery import ToolDiscovery
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.policy import PolicyEngine, PolicyDecision, default_policy_scope
    globals()["Request"] = Request

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    storage = output / "data"
    storage.mkdir(exist_ok=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    router_data = {
        "providers": {"local": {"driver": "openai_chat_completions", "api_key": "fixture-only", "base_url": base + "/fixture/v1", "retry": {"max_attempts": 1, "backoff_factor": 0}}},
        "models": {"fixture-model": {"provider": "local", "model_id": "fixture-model", "context_window": 32000, "tokenizer_encoding": "cl100k_base", "capabilities": ["chat", "function_calling"]}},
        "purposes": {"chat": {"priority": [{"model": "fixture-model", "priority": 1}]}},
    }
    import yaml
    (output / "model_router_config.yaml").write_text(yaml.safe_dump(router_data), encoding="utf-8")
    config = Config()
    config.session.directory = str(storage)
    config.tools.approval_commands = []
    config.tools.mcp_enabled = False
    registry = ToolRegistry()
    for handler in ToolDiscovery.discover_builtin():
        registry.register(handler)
    scope = default_policy_scope(workspace_root=str(output))
    scope.global_rules["process.exec"] = PolicyDecision.ALLOW
    executor = ToolExecutor(registry, policy_engine=PolicyEngine(scope), unattended_allow_profiles={"process.exec"})
    router = ModelRouter(load_router_config(output / "model_router_config.yaml"), RateLimiter({}), CircuitBreaker())
    proxy = LLMProxy(router)
    agents = AgentRegistry()
    parent = AgentIdentity(agent_id="parent-agent", agent_name="Benchmark Parent")
    agents.register(parent)
    agents.register(AgentIdentity(agent_id="target-agent", agent_name="Benchmark Child"))
    manager = SessionManager(storage)
    services = build_runtime_services(config=config, project_root=output, identity=parent, llm_proxy=proxy, tool_executor=executor, session_manager=manager, skill_registry=None, memory_manager=None, agent_registry=agents, preferred_model="fixture-model")
    kwargs = dict(session_manager=manager, agent_registry=agents, coordinator=services.coordinator, default_agent_id="parent-agent", run_repository=services.run_repository, approval_repository=services.approval_repository, context_port=services.context_port, preferred_model="fixture-model", chat_models=("fixture-model",))
    if "run_watch_service" in inspect.signature(SessionInteractionService).parameters:
        kwargs["run_watch_service"] = services.run_watch_service
    interaction = SessionInteractionService(**kwargs)
    host = ApplicationHost(config, output)
    host._llm_proxy, host._session_manager, host._agent_registry = proxy, manager, agents
    host._tool_executor, host._runtime_services = executor, services
    host._session_interaction, host._context_port = interaction, services.context_port

    async def ready_host():
        return host

    app = create_app(host_factory=ready_host, frontend_dir=source / "frontend" / "dist")
    state = {}
    repository = services.run_repository
    original_save = repository.save_run

    async def save_run(run):
        await original_save(run)
        if run.state.outcome() is not None and run.state.outcome().value == "cancelled":
            state.setdefault("terminal_commits", {})[run.run_id] = time.perf_counter()

    repository.save_run = save_run
    if hasattr(repository, "commit_cancellation"):
        original_commit = repository.commit_cancellation

        async def commit_cancellation(intent):
            await original_commit(intent)
            state.setdefault("terminal_commits", {})[intent.run.run_id] = time.perf_counter()

        repository.commit_cancellation = commit_cancellation

    async def observed_app(scope, receive, send):
        """纯 ASGI 观察不另建任务，避免改变旧版取消任务所有权。"""
        if scope["type"] == "http" and scope["path"].endswith("/cancel"):
            state["backend_receive"] = time.perf_counter()
            messages = []
            while True:
                message = await receive()
                messages.append(message)
                if not message.get("more_body", False):
                    break
            body = b"".join(item.get("body", b"") for item in messages)
            state["cancel_http_target"] = dict(session_id=scope["path"].split("/")[-2], run_id=json.loads(body).get("run_id"))
            async def replay_receive():
                if messages:
                    return messages.pop(0)
                return await receive()
            await app(scope, replay_receive, send)
        else:
            await app(scope, receive, send)

    # 底层驱动包装只记录退出时刻，保留原代理、适配器及驱动关闭行为。
    client = router.get_client("fixture-model")
    original_chat = client.chat

    async def timed_chat(*values, **options):
        sample_state = state
        task = asyncio.current_task()
        sample_state.setdefault("driver_tasks", set()).add(task)
        messages = options.get("messages", values[0] if values else [])
        last_user = next((item.content for item in reversed(messages) if item.role == "user"), "")
        try:
            async for chunk in original_chat(*values, **options):
                yield chunk
        finally:
            sample_state.setdefault("unrelated_exits" if "unrelated_wait" in last_user else "driver_exits", []).append(time.perf_counter())
            sample_state.get("driver_tasks", set()).discard(task)

    client.chat = timed_chat

    def packet(delta, finish=None):
        return "data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    @app.post("/fixture/v1/chat/completions")
    async def local_model(request: Request):
        sample_state = state
        payload = await request.json()
        messages = payload["messages"]
        text = next((str(item.get("content", "")) for item in reversed(messages) if item.get("role") == "user"), "")
        scenario = sample_state.get("scenario")
        is_child = "child_wait" in text
        followup = "followup-ok" in text
        unrelated = "unrelated_wait" in text
        sample_state.setdefault("provider_inputs", []).append(text)

        async def stream():
            if unrelated:
                sample_state["unrelated_started"] = time.perf_counter()
                try:
                    await asyncio.sleep(60)
                    yield packet({"content": "unrelated-finished"})
                finally:
                    sample_state["unrelated_provider_exit"] = time.perf_counter()
            elif followup:
                yield packet({"content": "followup-ok"})
            elif scenario == "command":
                sample_state["command_input"] = text
                yield packet({"tool_calls": [{"index": 0, "id": "command-call", "type": "function", "function": {"name": "builtin.process.execute", "arguments": json.dumps({"command": sample_state["command"]})}}]}, "tool_calls")
            elif scenario == "delegation" and not is_child:
                yield packet({"tool_calls": [{"index": 0, "id": "delegate-call", "type": "function", "function": {"name": "delegate", "arguments": json.dumps({"target_agent_id": "target-agent", "title": "固定子任务", "objective": "child_wait"})}}]}, "tool_calls")
            else:
                if scenario == "streaming":
                    yield packet({"content": "fixture-stream-start"})
                sample_state["barrier"] = time.perf_counter()
                sample_state["barrier_input"] = text
                try:
                    await asyncio.sleep(60)
                    yield packet({"content": "long-operation-finished"})
                finally:
                    sample_state["provider_exit"] = time.perf_counter()
            yield packet({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    # 真实静态站点挂载为兜底路由；测试供应商路由必须先于这个兜底匹配。
    app.router.routes.insert(0, app.router.routes.pop())

    # 本地任务只写固定 PID 证据，不接触用户文件或真实供应商。
    task_script = output / "long_command.py"
    task_script.write_text('import os,sys,time,subprocess,json\nfrom pathlib import Path\np=Path(sys.argv[1])\nif len(sys.argv)>2:\n p.write_text(str(os.getpid()))\n time.sleep(60)\nelse:\n child=subprocess.Popen([sys.executable,__file__,str(p.with_suffix(".child")),"child"])\n p.write_text(json.dumps([os.getpid(),child.pid]))\n time.sleep(60)\n', encoding="utf-8")
    server = uvicorn.Server(uvicorn.Config(observed_app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if server_task.done():
            await server_task
        await asyncio.sleep(.01)
    records = []
    async def fresh_session(page, text):
        """等原界面完整初始化和自动打开结束，再创建并核对独立目标会话。"""
        async with page.expect_response(lambda response: response.url == base + "/api/v1/sessions" and response.request.method == "GET") as sessions_response:
            await page.goto(base)
        initial_sessions = await (await sessions_response.value).json()
        await page.wait_for_function("""() => document.querySelector('.conversation') && !document.querySelector('.conversation .empty-note') && document.querySelector('#message-input')""")
        initial_observation = dict(session_count=len(initial_sessions), automatically_selected_session_id=await page.evaluate("JSON.parse(localStorage.getItem('dotclaw.last-session.v1') || 'null')"), conversation_excerpt=(await page.locator(".conversation").inner_text())[:600])
        await page.locator('button[aria-label="新对话"]').click()
        await page.locator(".welcome-title").wait_for()
        await page.wait_for_function("""() => localStorage.getItem('dotclaw.last-session.v1') === 'null'""")
        await page.locator("#message-input").fill(text)
        async with page.expect_response(lambda response: response.url == base + "/api/v1/sessions" and response.request.method == "POST") as created_response:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        session = await (await created_response.value).json()
        await page.wait_for_function("""id => JSON.parse(localStorage.getItem('dotclaw.last-session.v1') || 'null') === id""", arg=session["id"])
        initial_observation["created_session_id"] = session["id"]
        state.setdefault("initialization_observations", []).append(initial_observation)
        return session["id"]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=args.browser, headless=True)
        for scenario in args.scenarios.split(","):
            for attempt in range(args.count):
                context = await browser.new_context()
                page = await context.new_page()
                state = {"scenario": scenario}
                pidfile = output / f"{scenario}-{attempt}.pids"
                state["command"] = f'"{sys.executable}" "{task_script}" "{pidfile}"'
                record = dict(version=args.commit, scenario=scenario, phase=args.phase, round=args.round, attempt=attempt, imported_dotclaw=str(dotclaw.__file__), browser_clock="performance.now", backend_clock="time.perf_counter", terminal_to_page_ms=None, deadline_ms=10000, status="invalid_fixture", page_false_success=False, residual_resource=None, refresh_error=None, followup_success=False, unrelated_task_stopped=None)
                record.update(browser_click_ms=None, browser_dom_terminal_ms=None, backend_receive_s=None, backend_resource_exit_s=None, backend_terminal_commits_s={}, browser_click_to_terminal_ms=None, backend_receive_to_resource_exit_ms=None, backend_receive_to_terminal_commit_ms=None, cancel_success=False, resource_exit_evidence="owned_parent_and_child_pid_death" if scenario == "command" else "original_local_driver_finally", remote_resource_exit_confirmed=False)
                record["protocol_version"] = 2
                pids = []
                clicked = False
                barrier_ready = False
                missing_stop_entry = False
                other_context = await browser.new_context()
                other_page = await other_context.new_page()
                try:
                    other_session_id = await fresh_session(other_page, "unrelated_wait")
                    async with asyncio.timeout(12):
                        while not state.get("unrelated_started"):
                            await asyncio.sleep(.01)
                    target_input = f"benchmark-{scenario}-{args.phase}-{args.round}-{attempt}"
                    target_session_id = await fresh_session(page, target_input)
                    record.update(target_session_id=target_session_id, unrelated_session_id=other_session_id, target_input=target_input)
                    async with asyncio.timeout(12):
                        while True:
                            if scenario == "command" and pidfile.exists() and pidfile.with_suffix(".child").exists():
                                pids = json.loads(pidfile.read_text())
                                if all(pid_alive(pid) for pid in pids):
                                    state["barrier"] = time.perf_counter()
                                    break
                            elif scenario != "command" and state.get("barrier"):
                                break
                            await asyncio.sleep(.01)
                    target_run = await interaction.get_run(target_session_id)
                    assert target_run is not None and target_run.session_id == target_session_id
                    record["target_run_id"] = target_run.run_id
                    input_messages = await repository.load_messages(target_session_id, target_run.run_id)
                    assert any(message.content == target_input for message in input_messages), "后端目标运行输入必须属于本样本"
                    if scenario in ("before_first", "streaming"):
                        assert state["barrier_input"] == target_input, "模型等待屏障必须属于本样本目标"
                    elif scenario == "command":
                        assert state["command_input"] == target_input, "进程工具输入必须属于本样本目标"
                    else:
                        children = []
                        for session in await manager.list_all():
                            if session.agent_id == "target-agent":
                                child = await interaction.get_run(session.id)
                                if child is not None and child.parent_run_id == target_run.run_id:
                                    children.append(child)
                        assert len(children) == 1, "委派等待必须能追溯到当前父运行的唯一原版子运行"
                        child = children[0]
                        child_messages = await repository.load_messages(child.session_id, child.run_id)
                        assert any(message.content == state["barrier_input"] for message in child_messages), "委派屏障输入必须属于当前父运行的真实子运行"
                        assert state["barrier_input"] == "任务：固定子任务\n\n目标：child_wait", "委派屏障必须保留原版任务包装内容"
                        record.update(child_run_id=child.run_id, child_session_id=child.session_id)
                    barrier_ready = True
                    if scenario == "streaming":
                        await page.get_by_text("fixture-stream-start", exact=True).wait_for()
                    button = page.get_by_role("button", name="停止运行", exact=True)
                    # 委派挂起界面可能隐藏停止按钮，真实记录入口能力边界。
                    try:
                        await button.wait_for(timeout=3000)
                    except Exception:
                        missing_stop_entry = not await button.is_visible()
                        raise
                    await page.evaluate("""() => {window.__benchmark = {}; document.addEventListener('click', e => {if(e.target.closest('[aria-label="停止运行"]')) window.__benchmark.click = performance.now()}, true); const check = () => { const labels = [...document.querySelectorAll('.execution-status > span')]; if(window.__benchmark.click && !window.__benchmark.terminal && labels.some(x => x.textContent.trim() === '已停止') && document.querySelector('[aria-label="发送消息"]')) window.__benchmark.terminal = performance.now(); }; new MutationObserver(check).observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true}); }""")
                    await button.click()
                    clicked = True
                    deadline = time.perf_counter() + 10
                    resource_exit = None
                    while time.perf_counter() < deadline:
                        dom = await page.evaluate("window.__benchmark")
                        if scenario == "command":
                            if resource_exit is None and not any(pid_alive(pid) for pid in pids):
                                resource_exit = time.perf_counter()
                        elif state.get("driver_exits"):
                            # 委派场景只接受屏障之后的子调用退出。
                            exits = [value for value in state["driver_exits"] if value >= state["barrier"]]
                            if exits:
                                resource_exit = max(exits)
                        cancelled = state.get("terminal_commits", {})
                        http_target = state.get("cancel_http_target", {})
                        # 旧版原界面只传会话，新版还传运行；均核对冻结的原目标提交。
                        target_matches = http_target.get("session_id") == target_session_id and http_target.get("run_id") in (None, target_run.run_id)
                        if dom.get("terminal") and dom["terminal"] - dom["click"] <= 10000 and target_matches and target_run.run_id in cancelled and resource_exit:
                            record["status"] = "success"
                            break
                        await asyncio.sleep(.01)
                    else:
                        record["status"] = "timeout"
                    dom = await page.evaluate("window.__benchmark")
                    received_at = state.get("backend_receive")
                    commits = state.get("terminal_commits", {})
                    record["cancel_http_target"] = state.get("cancel_http_target")
                    record["target_commit_confirmed"] = target_run.run_id in commits
                    persisted = await repository.load_run(target_session_id, target_run.run_id)
                    record["persisted_target_outcome"] = persisted.state.outcome().value if persisted and persisted.state.outcome() else None
                    if record["status"] == "success" and record["persisted_target_outcome"] != "cancelled":
                        record["status"] = "failure"
                    record.update(browser_click_ms=dom.get("click"), browser_dom_terminal_ms=dom.get("terminal"), backend_receive_s=received_at, backend_resource_exit_s=resource_exit, backend_terminal_commits_s=commits.copy(), resource_pids=pids, barrier_s=state["barrier"], driver_exits_s=list(state.get("driver_exits", [])), provider_exit_s=state.get("provider_exit"), browser_click_to_terminal_ms=(dom["terminal"]-dom["click"]) if dom.get("terminal") else None, backend_receive_to_resource_exit_ms=(resource_exit-received_at)*1000 if received_at and resource_exit else None, backend_receive_to_terminal_commit_ms=(commits[target_run.run_id]-received_at)*1000 if received_at and target_run.run_id in commits else None)
                    record["residual_resource"] = any(pid_alive(pid) for pid in pids) if pids else resource_exit is None
                    record["page_false_success"] = bool(dom.get("terminal") and (not commits or record["residual_resource"]))
                    record["cancel_success"] = record["status"] == "success"
                    record["unrelated_task_stopped"] = bool(state.get("unrelated_exits") or state.get("unrelated_provider_exit"))
                    await page.screenshot(path=str(output / f"{scenario}-{attempt}.png"))
                    await page.reload()
                    try:
                        await page.locator(".execution-status").filter(has_text="已停止").first.wait_for(timeout=3000)
                        record["refresh_error"] = False
                    except Exception:
                        record["refresh_error"] = True
                        record["refresh_dom_excerpt"] = (await page.locator(".conversation").inner_text())[:1500]
                    await page.locator("#message-input").fill("followup-ok")
                    try:
                        await page.get_by_role("button", name="发送消息", exact=True).click(timeout=1500)
                        await page.wait_for_function("""() => [...document.querySelectorAll('.message--assistant')].some(x => x.textContent.includes('followup-ok') && x.querySelector('.execution-status')?.textContent.includes('已完成'))""", timeout=10000)
                        record["followup_success"] = True
                    except Exception as followup_error:
                        record["followup_success"] = False
                        record["followup_error"] = repr(followup_error)
                        record["followup_dom_excerpt"] = (await page.locator(".conversation").inner_text())[:1500]
                    await page.screenshot(path=str(output / f"{scenario}-{attempt}-refreshed.png"))
                    if record["status"] == "success" and (record["refresh_error"] or not record["followup_success"] or record["unrelated_task_stopped"]):
                        record["status"] = "functional_failure"
                except Exception as error:
                    record.update(status=classify_failure(clicked, barrier_ready, missing_stop_entry), error=repr(error), traceback=traceback.format_exc(), cancel_success=False, barrier_s=state.get("barrier"), resource_pids=pids, unrelated_task_stopped=bool(state.get("unrelated_exits") or state.get("unrelated_provider_exit")))
                    await page.screenshot(path=str(output / f"{scenario}-{attempt}-error.png"))
                    record["fixture_state_keys"] = sorted(state)
                    record["provider_inputs"] = list(state.get("provider_inputs", []))
                    if barrier_ready and not clicked:
                        record["entry_dom_excerpt"] = (await page.locator(".conversation").inner_text())[:1500]
                        await page.reload()
                        try:
                            await page.locator(".execution-status").filter(has_text="已停止").first.wait_for(timeout=3000)
                            record["refresh_error"] = False
                        except Exception:
                            record["refresh_error"] = True
                        await page.locator("#message-input").fill("followup-ok")
                        try:
                            await page.get_by_role("button", name="发送消息", exact=True).click(timeout=1500)
                            await page.wait_for_function("""() => [...document.querySelectorAll('.message--assistant')].some(x => x.textContent.includes('followup-ok') && x.querySelector('.execution-status')?.textContent.includes('已完成'))""", timeout=10000)
                            record["followup_success"] = True
                        except Exception:
                            record["followup_success"] = False
                finally:
                    # 先将受测观察写入原始样本，再对本样本归属进程进行隔离清理。
                    record["sample_frozen_s"] = time.perf_counter()
                    record["initialization_observations"] = list(state.get("initialization_observations", []))
                    record["overall_success"] = record["status"] == "success"
                    with (output / "samples.jsonl").open("a", encoding="utf-8") as raw:
                        raw.write(json.dumps(record, ensure_ascii=False) + "\n")
                    records.append(record)
                    print(json.dumps({key: record.get(key) for key in ("version", "scenario", "phase", "attempt", "status", "browser_click_to_terminal_ms", "error")}, ensure_ascii=False), flush=True)
                    for pid in pids:
                        if pid_alive(pid):
                            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                    # 对照任务和超时任务也只在样本冻结后由测试端隔离清理。
                    pending = tuple(state.get("driver_tasks", set()))
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.wait(pending, timeout=3)
                    await context.close()
                    await other_context.close()
        await browser.close()
    # 测量结束后关闭残留模型连接，不能进入成功指标。
    for task in tuple(app.state.submission_tasks):
        task.cancel()
    server.should_exit = True
    try:
        await asyncio.wait_for(server_task, 8)
    except TimeoutError:
        server.force_exit = True
        server_task.cancel()
    listener.close()
    (output / "environment.json").write_text(json.dumps(dict(executable=sys.executable, python=sys.version, source=str(source), imported_dotclaw=str(dotclaw.__file__), browser=args.browser, browser_version=browser.version, source_commit=args.commit), indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "output", "commit", "browser", "phase"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--scenarios", default="before_first,streaming,command,delegation")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--round", type=int, default=0)
    asyncio.run(main(parser.parse_args()))
