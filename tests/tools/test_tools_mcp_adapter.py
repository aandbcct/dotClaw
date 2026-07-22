"""阶段四：MCP Tool Adapter 测试（命名空间名、统一结果/错误码、参数校验隔离）。

不依赖真实 MCP server：使用轻量 FakeMcpClient  duck-type 适配层所需的接口。
"""

from __future__ import annotations

import asyncio

from dotclaw.mcp.client import McpToolInfo, McpToolResult, McpClientState, McpUnavailableError
from dotclaw.mcp.tool_adapter import McpToolAdapter, mcp_tool_name
from dotclaw.tools.base import ToolSource, ToolErrorCode, ToolErrorType


class FakeMcpClient:
    """最小 MCP client 替身，仅实现适配层与 Provider 使用的接口。"""

    def __init__(
        self,
        server_name: str,
        *,
        connect_ok: bool = True,
        call_result: McpToolResult | None = None,
        raise_on_call=None,
        state: McpClientState = McpClientState.CONNECTED,
    ):
        self._server_name = server_name
        self._connect_ok = connect_ok
        self._call_result = call_result
        self._raise_on_call = raise_on_call
        self._state = state if connect_ok else McpClientState.FAILED

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def state(self) -> McpClientState:
        return self._state

    def get_tool_timeout(self) -> float:
        return 30.0

    def get_startup_timeout(self) -> float:
        return 10.0

    async def connect(self) -> bool:
        self._state = McpClientState.CONNECTED
        return self._connect_ok

    async def call_tool(self, name, arguments, timeout=None) -> McpToolResult:
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._server_name} 不可用（state={self._state}）")
        if self._raise_on_call is not None:
            raise self._raise_on_call
        if self._call_result is not None:
            return self._call_result
        return McpToolResult(content=f"result:{name}")

    async def shutdown(self):
        self._state = McpClientState.SHUTDOWN


def _tool(name: str, schema: dict | None = None) -> McpToolInfo:
    return McpToolInfo(name=name, description=f"desc {name}", input_schema=schema or {})


def test_registered_name_is_namespaced():
    client = FakeMcpClient("s1")
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema={})
    assert adapter.definition().name == "mcp.s1.foo"
    assert adapter.definition().source is ToolSource.MCP


def test_two_servers_same_original_name_do_not_conflict():
    a = McpToolAdapter(FakeMcpClient("s1"), name="foo", description="d", input_schema={})
    b = McpToolAdapter(FakeMcpClient("s2"), name="foo", description="d", input_schema={})
    assert a.definition().name == "mcp.s1.foo"
    assert b.definition().name == "mcp.s2.foo"
    assert a.definition().name != b.definition().name


def test_original_tool_name_preserved_for_call():
    client = FakeMcpClient("s1")
    adapter = McpToolAdapter(client, name="getWeather", description="d", input_schema={})

    async def run():
        result = await adapter.execute({"city": "BJ"})
        return result

    outcome = asyncio.run(run())
    # 协议调用使用原始名 getWeather；注册名带命名空间。
    assert outcome.output == "result:getWeather"
    assert adapter.definition().metadata["mcp_tool_name"] == "getWeather"


def test_definition_carries_mcp_policy_and_server():
    client = FakeMcpClient("s1")
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema={}, needs_approval=True)
    d = adapter.definition()
    assert d.policy_profile == "mcp.call"
    assert d.metadata["server"] == "s1"
    assert d.needs_approval is True


def test_input_schema_exposed_for_executor_validation():
    client = FakeMcpClient("s1")
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema=schema)
    # input_schema 非 None，执行器据此走 JSON Schema 校验分支。
    assert adapter.input_schema == schema


def test_success_maps_to_unified_result():
    client = FakeMcpClient("s1", call_result=McpToolResult(content="ok", is_error=False))
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema={})

    async def run():
        return await adapter.execute({})

    out = asyncio.run(run())
    assert out.output == "ok"
    assert out.is_error is False
    assert out.error_code is None


def test_mcp_unavailable_maps_to_error_code():
    client = FakeMcpClient("s1", state=McpClientState.FAILED)
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema={})

    async def run():
        return await adapter.execute({})

    out = asyncio.run(run())
    assert out.is_error is True
    assert out.error_code == ToolErrorCode.MCP_UNAVAILABLE
    assert out.error_type == ToolErrorType.MCP_UNAVAILABLE


def test_timeout_maps_to_error_code():
    client = FakeMcpClient("s1", raise_on_call=asyncio.TimeoutError())
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema={})

    async def run():
        return await adapter.execute({})

    out = asyncio.run(run())
    assert out.is_error is True
    assert out.error_code == ToolErrorCode.TIMEOUT


def test_execution_error_maps_to_error_code():
    client = FakeMcpClient("s1", raise_on_call=RuntimeError("boom"))
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema={})

    async def run():
        return await adapter.execute({})

    out = asyncio.run(run())
    assert out.is_error is True
    assert out.error_code == ToolErrorCode.EXECUTION_ERROR
