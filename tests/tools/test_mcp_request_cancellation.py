"""使用安装的真实 SDK 和本地 stdio 对端验证请求取消隔离。"""

import asyncio
import sys
import socket

import pytest

from dotclaw.config.settings import McpServerConfig, McpGlobalConfig
from dotclaw.mcp.client import McpClient, McpClientState


SERVER = '''import asyncio,json,sys
from pathlib import Path
root=Path(sys.argv[1]); tasks={}
def send(message):
 print(json.dumps(message),flush=True)
async def answer(request):
 rid=request["id"]; method=request["method"]; params=request.get("params",{})
 if method=="initialize":
  result={"protocolVersion":params.get("protocolVersion","2025-11-25"),"capabilities":{"tools":{},"resources":{},"prompts":{}},"serverInfo":{"name":"cancel-test","version":"1"}}
 elif method=="tools/list":
  result={"tools":[{"name":"wait","description":"controlled wait","inputSchema":{"type":"object","properties":{"label":{"type":"string"},"delay":{"type":"number"}}}}]}
 elif method=="resources/list": result={"resources":[]}
 elif method=="prompts/list": result={"prompts":[]}
 elif method=="tools/call":
  args=params["arguments"]; label=args["label"]
  (root/(label+".started")).write_text(str(rid))
  try: await asyncio.sleep(args.get("delay",0))
  except asyncio.CancelledError:
   (root/(label+".cancelled")).write_text(str(rid)); return
  result={"content":[{"type":"text","text":"done:"+label}],"isError":False}
 else: result={}
 send({"jsonrpc":"2.0","id":rid,"result":result})
async def main():
 while True:
  line=await asyncio.to_thread(sys.stdin.readline)
  if not line: break
  request=json.loads(line)
  if request.get("method")=="notifications/cancelled":
   rid=request["params"]["requestId"]
   if rid in tasks: tasks[rid].cancel()
  elif "id" in request:
   tasks[request["id"]]=asyncio.create_task(answer(request))
 for task in tasks.values(): task.cancel()
 await asyncio.gather(*tasks.values(),return_exceptions=True)
asyncio.run(main())
'''


async def _wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("cancel_mode", ["cancel", "timeout"])
async def test_real_stdio_sdk_cancels_one_request_and_keeps_connection(tmp_path, cancel_mode) -> None:
    script = tmp_path / "mcp_server.py"
    script.write_text(SERVER, encoding="utf-8")
    client = McpClient(McpServerConfig(name="local", command=sys.executable,
        args=[str(script), str(tmp_path)], startup_timeout=5, restart_on_crash=False), McpGlobalConfig())
    assert await client.connect()
    connection = client._connection_task
    first = second = None
    try:
        first = asyncio.create_task(client.call_tool("wait", {"label": "first", "delay": 20}, timeout=.4 if cancel_mode == "timeout" else 5))
        second = asyncio.create_task(client.call_tool("wait", {"label": "second", "delay": .7}, timeout=5))
        await _wait_file(tmp_path / "first.started")
        await _wait_file(tmp_path / "second.started")
        if cancel_mode == "cancel":
            first.cancel()
        with pytest.raises(asyncio.CancelledError if cancel_mode == "cancel" else TimeoutError):
            await first
        await _wait_file(tmp_path / "first.cancelled")
        assert (tmp_path / "first.cancelled").read_text() == (tmp_path / "first.started").read_text()
        assert not (tmp_path / "second.cancelled").exists()
        assert (await second).content == "done:second"
        assert client.state is McpClientState.CONNECTED
        assert client._connection_task is connection and not connection.done()
        assert (await client.call_tool("wait", {"label": "third"}, timeout=3)).content == "done:third"
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        await client.shutdown()
        await client.shutdown()
    assert connection.done()
    assert client._session is None and client._transport is None


async def test_failed_stdio_startup_releases_partial_connection(tmp_path) -> None:
    client = McpClient(McpServerConfig(name="bad", command=str(tmp_path / "missing-server.exe"),
        startup_timeout=1, restart_on_crash=False), McpGlobalConfig())
    assert not await client.connect()
    assert client.state is McpClientState.FAILED
    assert client._connection_task is None and client._session is None
    await client.shutdown()


async def test_stdio_handshake_timeout_reaps_real_started_process(tmp_path) -> None:
    from tests.tools.test_process_supervisor import _alive
    script, marker = tmp_path / "silent.py", tmp_path / "silent.pid"
    script.write_text("import os,time\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").write_text(str(os.getpid()))\ntime.sleep(30)\n", encoding="utf-8")
    client = McpClient(McpServerConfig(name="silent", command=sys.executable,
        args=[str(script)], startup_timeout=.5, restart_on_crash=False), McpGlobalConfig())
    assert not await asyncio.wait_for(client.connect(), 5)
    assert marker.exists()
    pid = int(marker.read_text())
    assert not _alive(pid)
    assert client._connection_task is None and client._session is None
    await client.shutdown()


async def test_real_http_sdk_cancellation_preserves_other_request_and_session() -> None:
    """真实本地 HTTP SDK 对端验证取消关联，不以 stdio 结果替代 HTTP。"""
    import uvicorn
    from mcp.server import MCPServer
    server = MCPServer("cancel-http")
    started, cancelled = asyncio.Event(), asyncio.Event()
    @server.tool()
    async def wait(label: str, delay: float = 0) -> str:
        if label == "first":
            started.set()
        try:
            await asyncio.sleep(delay)
            return "done:" + label
        except asyncio.CancelledError:
            if label == "first":
                cancelled.set()
            raise
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    host = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="warning", access_log=False))
    host_task = asyncio.create_task(host.serve(sockets=[listener]))
    client = McpClient(McpServerConfig(name="http", transport="streamable_http",
        url=f"http://127.0.0.1:{port}/mcp", startup_timeout=5, restart_on_crash=False), McpGlobalConfig())
    first = second = None
    try:
        async with asyncio.timeout(5):
            while not host.started:
                await asyncio.sleep(.01)
        assert await client.connect()
        connection = client._connection_task
        first = asyncio.create_task(client.call_tool("wait", {"label": "first", "delay": 20}, timeout=5))
        second = asyncio.create_task(client.call_tool("wait", {"label": "second", "delay": .3}, timeout=5))
        await asyncio.wait_for(started.wait(), 3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(cancelled.wait(), 3)
        assert (await second).content == "done:second"
        assert client.state is McpClientState.CONNECTED and client._connection_task is connection
        assert (await client.call_tool("wait", {"label": "third"}, timeout=3)).content == "done:third"
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        await client.shutdown()
        host.should_exit = True
        await asyncio.wait_for(host_task, 5)
        listener.close()
