"""不可强杀的真实文件线程必须完成并关闭后，调用才允许退出。"""

import asyncio
import builtins
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from dotclaw.tools.base import ToolContext
from dotclaw.tools.builtin import file_tool, memory_tool


@pytest.mark.parametrize("operation", ["read", "write", "memory_read", "memory_write"])
async def test_repeated_cancel_waits_for_file_thread_and_actual_close(tmp_path, monkeypatch, operation):
    path = tmp_path / "owned.txt"
    path.write_text("seed", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    streams = []
    @contextmanager
    def blocked_open(*args, **kwargs):
        with builtins.open(*args, **kwargs) as stream:
            streams.append(stream)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试没有释放文件I/O屏障")
            yield stream
    module = memory_tool if operation.startswith("memory") else file_tool
    monkeypatch.setattr(module, "open", blocked_open, raising=False)
    calls = {
        "read": lambda: file_tool.read_text(str(path), context=ToolContext()),
        "write": lambda: file_tool.write_text(str(path), "new", context=ToolContext()),
        "memory_read": lambda: memory_tool.read(str(path), context=ToolContext()),
        "memory_write": lambda: memory_tool.write("new", str(path), context=ToolContext()),
    }
    task = asyncio.create_task(calls[operation]())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and streams and not streams[0].closed
        # 工作线程仍阻塞时事件循环可以处理下一项任务；不会阻塞取消受理。
        assert await asyncio.sleep(0, result="responsive") == "responsive"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(stream.closed for stream in streams)
    expected = "new" if operation == "write" else "seed\nnew\n" if operation == "memory_write" else "seed"
    assert path.read_text(encoding="utf-8") == expected


async def test_directory_enumeration_is_off_loop_and_waits_for_worker(tmp_path, monkeypatch):
    (tmp_path / "entry.txt").write_text("data", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original = Path.iterdir
    def blocked(path):
        if path == tmp_path:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试没有释放目录I/O屏障")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", blocked)
    task = asyncio.create_task(file_tool.list_directory(str(tmp_path), context=ToolContext()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
