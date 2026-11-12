"""真实父子孙进程、继承管道和调用隔离的跨平台资源验收。"""

from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dotclaw.tools.process_supervisor import ProcessSupervisor
from dotclaw.tools.builtin.exec_tool import execute
from dotclaw.tools.base import ToolContext
from dotclaw.common.resource_errors import ResourceCleanupError


@pytest.mark.parametrize("value", [-1, float("inf"), float("-inf"), float("nan"), True, None, "1"])
def test_termination_grace_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="非负有限数"):
        ProcessSupervisor(terminate_grace_seconds=value)


def test_termination_grace_default_zero_and_keyword_contract():
    assert ProcessSupervisor()._terminate_grace_seconds == 1.0
    assert ProcessSupervisor(terminate_grace_seconds=0)._terminate_grace_seconds == 0
    with pytest.raises(TypeError):
        ProcessSupervisor(.1)


TREE = '''import os,sys,time,subprocess
from pathlib import Path
depth=int(sys.argv[1]); root=Path(sys.argv[2]); root.mkdir(parents=True,exist_ok=True)
(root / (str(depth)+".pid")).write_text(str(os.getpid()))
if depth:
 subprocess.Popen([sys.executable,__file__,str(depth-1),str(root)])
while True:
 with (root / (str(depth)+".beat")).open("a") as stream: stream.write("x"); stream.flush()
 time.sleep(.02)
'''


def _command(script: Path, depth: int, directory: Path) -> str:
    args = [sys.executable, str(script), str(depth), str(directory)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


async def _ready(directory: Path, count: int) -> tuple[int, ...]:
    async with asyncio.timeout(5):
        while True:
            files = tuple(directory.glob("*.pid"))
            if len(files) == count:
                try:
                    return tuple(int(path.read_text()) for path in sorted(files))
                except ValueError:
                    pass
            await asyncio.sleep(.01)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import _winapi
        try:
            handle = _winapi.OpenProcess(0x100000, False, pid)
        except OSError:
            return False
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split(") ", 1)[1].split()[0] == "Z":
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _assert_stopped(pids: tuple[int, ...], directory: Path) -> None:
    async with asyncio.timeout(3):
        while any(_alive(pid) for pid in pids):
            await asyncio.sleep(.01)
    before = {path.name: path.stat().st_size for path in directory.glob("*.beat")}
    await asyncio.sleep(.12)
    assert before == {path.name: path.stat().st_size for path in directory.glob("*.beat")}


@pytest.mark.parametrize("sample", range(20))
async def test_cancel_stops_parent_child_grandchild_and_preserves_other_call(tmp_path, record_property, sample) -> None:
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    target, other = tmp_path / "target", tmp_path / "other"
    target.mkdir(); other.mkdir()
    task = asyncio.create_task(ProcessSupervisor().run(_command(script, 2, target), 15))
    unrelated = asyncio.create_task(ProcessSupervisor().run(_command(script, 0, other), 15))
    pids = other_pids = ()
    try:
        pids = await _ready(target, 3)
        other_pids = await _ready(other, 1)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - started
        assert elapsed < 3
        await _assert_stopped(pids, target)
        assert all(_alive(pid) for pid in other_pids) and not unrelated.done()
        first = (other / "0.beat").stat().st_size
        await asyncio.sleep(.08)
        assert (other / "0.beat").stat().st_size > first
        record_property("resource_evidence", json.dumps({"platform": sys.platform, "pids": pids,
            "unrelated_pids": other_pids, "cleanup_seconds": elapsed, "living_after_cleanup": 0}))
    finally:
        for running in (task, unrelated):
            if not running.done():
                running.cancel()
        await asyncio.gather(task, unrelated, return_exceptions=True)
    await _assert_stopped(other_pids, other)


async def test_timeout_reclaims_tree_and_inherited_output_pipe(tmp_path) -> None:
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    directory = tmp_path / "tree"
    directory.mkdir()
    task = asyncio.create_task(ProcessSupervisor().run(_command(script, 2, directory), 1.5))
    pids = await _ready(directory, 3)
    with pytest.raises(TimeoutError):
        await task
    await _assert_stopped(pids, directory)


@pytest.mark.parametrize("cause", ["cancel", "timeout"])
async def test_cancel_during_creation_retains_resource_then_cleans_once(tmp_path, monkeypatch, cause) -> None:
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    directory = tmp_path / "tree"
    directory.mkdir()
    supervisor = ProcessSupervisor()
    created, release = asyncio.Event(), asyncio.Event()
    original = supervisor._spawn_owned
    clean_original = supervisor._terminate_owned
    cleaned = []
    async def spawn(command):
        owned = await original(command)
        created.set()
        await release.wait()
        return owned
    async def terminate(owned):
        cleaned.append(owned.pid)
        await clean_original(owned)
    monkeypatch.setattr(supervisor, "_spawn_owned", spawn)
    monkeypatch.setattr(supervisor, "_terminate_owned", terminate)
    task = asyncio.create_task(supervisor.run(_command(script, 2, directory), .15 if cause == "timeout" else 15))
    await created.wait()
    pids = await _ready(directory, 3)
    if cause == "cancel":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    else:
        await asyncio.sleep(.2)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError if cause == "cancel" else TimeoutError):
        await task
    assert len(cleaned) == 1
    await _assert_stopped(pids, directory)


async def test_natural_exit_and_context_timeout_are_respected() -> None:
    assert "natural-success" in await ProcessSupervisor().run("echo natural-success", 3)
    with pytest.raises(TimeoutError):
        await execute(f'"{sys.executable}" -c "import time;time.sleep(5)"', context=ToolContext(timeout=.1))


async def test_shell_exit_does_not_hide_descendants_holding_stdout(tmp_path) -> None:
    script = tmp_path / "tree.py"
    script.write_text(TREE.replace("while True:", "if depth==2: sys.exit(0)\nwhile True:"), encoding="utf-8")
    directory = tmp_path / "tree"
    directory.mkdir()
    task = asyncio.create_task(ProcessSupervisor().run(_command(script, 2, directory), 15))
    pids = await _ready(directory, 3)
    async with asyncio.timeout(2):
        while _alive(pids[2]):
            await asyncio.sleep(.01)
    assert not task.done() and _alive(pids[0]) and _alive(pids[1])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_stopped(pids, directory)


async def test_cleanup_failure_is_explicit_even_after_resources_are_killed(tmp_path, monkeypatch) -> None:
    supervisor = ProcessSupervisor()
    original = supervisor._terminate_owned
    async def failed(owned):
        await original(owned)
        raise ResourceCleanupError("process_tree", "cleanup_verification_failed", "清理校验失败")
    monkeypatch.setattr(supervisor, "_terminate_owned", failed)
    with pytest.raises(ResourceCleanupError, match="清理校验失败"):
        await supervisor.run("echo done", 3)


@pytest.mark.skipif(os.name != "nt", reason="真实 Runtime 安全审批链集成在 Windows 环境执行；Linux 本文件其余测试验证同一监管器")
async def test_real_runtime_approved_command_cancellation_reclaims_entire_tree(tmp_path) -> None:
    notifier = RunChangeNotifier()
    from types import SimpleNamespace
    from tests.runtime_v2.test_runtime_engine import _engine, _request
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.function_handler import FunctionToolHandler
    from dotclaw.tools.decorator import get_tool_meta
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.runtime.adapters.tool_executor_adapter import ToolExecutorAdapter
    from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
    from dotclaw.runtime.domain.facts import ToolCall, RunMessage, RunMessageKind, MessageRole
    from dotclaw.runtime.domain.state import RunOutcome
    from dotclaw.runtime.domain.cancellation import CancellationEffect
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    directory = tmp_path / "tree"
    directory.mkdir()
    registry = ToolRegistry()
    registry.register(FunctionToolHandler(execute, get_tool_meta(execute)))
    engine = _engine(tmp_path, run_change_notifier=notifier)
    engine._tool_port = ToolExecutorAdapter(ToolExecutor(registry))
    model_calls = []
    async def model(*args):
        model_calls.append(True)
        return RunMessage("tools", 0, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
            tool_calls=(ToolCall("command", "builtin.process.execute", {"command": _command(script, 2, directory)}),))
    engine._llm_port = SimpleNamespace(complete=model)
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    waiting = await coordinator.submit(_request("runtime-process"))
    assert waiting.state.is_waiting_approval()
    driver = asyncio.create_task(coordinator.resolve_approval(waiting.approval_id, True))
    pids = await _ready(directory, 3)
    try:
        await coordinator.cancel(waiting.run_id, "停止真实命令")
        assert (await driver).state.outcome() is RunOutcome.CANCELLED
        await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
        record = await engine._run_repository.load_cancellation("runtime-process", waiting.run_id)
        assert record.effect is CancellationEffect.LOCAL_ONLY
        assert len(model_calls) == 1
        await _assert_stopped(pids, directory)
    finally:
        if not driver.done():
            await coordinator.cancel(waiting.run_id, "测试收尾")
            await driver


@pytest.mark.skipif(os.name != "nt", reason="只核实 Windows 作业登记早于线程恢复")
async def test_windows_ownership_failure_cannot_run_user_command(tmp_path, monkeypatch) -> None:
    from dotclaw.tools import process_supervisor as module
    marker = tmp_path / "must-not-exist"
    monkeypatch.setattr(module._kernel, "AssignProcessToJobObject", lambda *args: False)
    with pytest.raises(OSError):
        await ProcessSupervisor().run(f'echo bad > "{marker}"', 3)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows 不伪造 POSIX 优雅终止信号")
async def test_posix_grace_allows_entire_group_to_exit_without_sigkill(tmp_path, monkeypatch):
    import signal
    from dotclaw.tools import process_supervisor as module
    script = tmp_path / "graceful.py"
    script.write_text('''import os,signal,sys,time
from pathlib import Path
root=Path(sys.argv[1])
def stop(*args):
 (root/"term").write_text("received")
 time.sleep(.12)
 sys.exit(0)
signal.signal(signal.SIGTERM,stop)
(root/"0.pid").write_text(str(os.getpid()))
while True: time.sleep(.01)
''', encoding="utf-8")
    original = module.os.killpg
    signals = []
    def record(group, sig):
        if sig:
            signals.append(sig)
        return original(group, sig)
    monkeypatch.setattr(module.os, "killpg", record)
    args = shlex.join([sys.executable, str(script), str(tmp_path)])
    task = asyncio.create_task(ProcessSupervisor(terminate_grace_seconds=.8).run(args, 5))
    await _ready(tmp_path, 1)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    assert (tmp_path / "term").read_text() == "received"
    assert signals == [signal.SIGTERM]
    assert .1 <= elapsed < .7


@pytest.mark.skipif(os.name == "nt", reason="Windows 使用作业对象终止，不发送 SIGTERM/SIGKILL")
@pytest.mark.parametrize("grace", [0, .2])
async def test_posix_stubborn_group_gets_sigkill_only_after_configured_grace(tmp_path, monkeypatch, grace):
    import signal
    from dotclaw.tools import process_supervisor as module
    script = tmp_path / "stubborn.py"
    script.write_text('''import os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
(Path(sys.argv[1])/"0.pid").write_text(str(os.getpid()))
while True: time.sleep(.01)
''', encoding="utf-8")
    original = module.os.killpg
    calls = []
    def record(group, sig):
        if sig:
            calls.append((sig, time.monotonic()))
        return original(group, sig)
    monkeypatch.setattr(module.os, "killpg", record)
    task = asyncio.create_task(ProcessSupervisor(terminate_grace_seconds=grace).run(
        shlex.join([sys.executable, str(script), str(tmp_path)]), 5))
    pids = await _ready(tmp_path, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [sig for sig, _ in calls] == [signal.SIGTERM, signal.SIGKILL]
    assert calls[1][1] - calls[0][1] >= max(0, grace - .02)
    assert not any(_alive(pid) for pid in pids)
