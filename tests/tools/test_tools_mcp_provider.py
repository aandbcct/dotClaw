"""阶段四：MCP Provider 测试（仅注册 tools、命名空间、mcp.connect 网关、状态、降级）。

不依赖真实 MCP server：用 FakeMcpClient + client_factory 注入。
"""

from __future__ import annotations

import asyncio

from dotclaw.mcp.client import McpToolInfo, McpToolResult, McpClientState
from dotclaw.mcp.provider import MCPToolProvider
from dotclaw.mcp.tool_adapter import McpToolAdapter
from dotclaw.tools.registry import ToolRegistry
from dotclaw.tools.policy import PolicyEngine, default_policy_scope


class FakeServerConfig:
    def __init__(self, name: str):
        self.name = name

    def get_tool_timeout(self, global_cfg=None) -> float:
        return 30.0

    def get_startup_timeout(self, global_cfg=None) -> float:
        return 10.0

    def get_restart_on_crash(self, global_cfg=None) -> bool:
        return False

    def get_max_restart_attempts(self, global_cfg=None) -> int:
        return 1


class FakeMcpClient:
    def __init__(self, server_name: str, *, tools=None, resources=None, prompts=None, connect_ok=True, call_result=None):
        self._server_name = server_name
        self._tools = tools or []
        self._resources = resources or []
        self._prompts = prompts or []
        self._connect_ok = connect_ok
        self._call_result = call_result
        self._state = McpClientState.CONNECTED if connect_ok else McpClientState.FAILED

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def tools(self):
        return self._tools

    @property
    def resources(self):
        return self._resources

    @property
    def prompts(self):
        return self._prompts

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
            from dotclaw.mcp.client import McpUnavailableError
            raise McpUnavailableError(f"MCP server {self._server_name} 不可用")
        if self._call_result is not None:
            return self._call_result
        return McpToolResult(content=f"result:{name}")

    async def shutdown(self):
        self._state = McpClientState.SHUTDOWN


def _tool(name: str, schema: dict | None = None) -> McpToolInfo:
    return McpToolInfo(name=name, description=f"d {name}", input_schema=schema or {})


def _factory(tool_map: dict[str, list], *, failed: set[str] | None = None):
    failed = failed or set()

    def _f(cfg, global_cfg):
        ok = cfg.name not in failed
        return FakeMcpClient(
            cfg.name,
            tools=tool_map.get(cfg.name, []),
            resources=[_tool("res1")],
            prompts=[_tool("pr1")],
            connect_ok=ok,
        )

    return _f


def test_only_tools_registered_namespaced_no_resources_prompts():
    registry = ToolRegistry()
    tool_map = {"s1": [_tool("foo"), _tool("bar")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("s1")],
        registry=registry,
        client_factory=_factory(tool_map),
    )

    async def run():
        return await provider.start()

    names = asyncio.run(run())
    assert set(names) == {"mcp.s1.foo", "mcp.s1.bar"}
    # resources / prompts 未注册为工具。
    assert all(n.startswith("mcp.") for n in registry.all_names())
    assert not any("res1" in n or "pr1" in n for n in registry.all_names())


def test_two_servers_same_original_name_no_conflict():
    registry = ToolRegistry()
    tool_map = {"s1": [_tool("foo")], "s2": [_tool("foo")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("s1"), FakeServerConfig("s2")],
        registry=registry,
        client_factory=_factory(tool_map),
    )

    async def run():
        return await provider.start()

    names = asyncio.run(run())
    assert set(names) == {"mcp.s1.foo", "mcp.s2.foo"}


def test_connection_failure_does_not_block_agent():
    registry = ToolRegistry()
    # s2 连接失败，s1 成功；Agent 不应被阻塞。
    tool_map = {"s1": [_tool("foo")], "s2": [_tool("bar")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("s1"), FakeServerConfig("s2")],
        registry=registry,
        client_factory=_factory(tool_map, failed={"s2"}),
    )

    async def run():
        return await provider.start()

    names = asyncio.run(run())
    assert names == ["mcp.s1.foo"]
    assert "s2" in provider.failed_servers
    assert "s1" in provider.clients


def test_server_states_observable():
    registry = ToolRegistry()
    tool_map = {"s1": [_tool("foo")], "s2": [_tool("bar")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("s1"), FakeServerConfig("s2")],
        registry=registry,
        client_factory=_factory(tool_map, failed={"s2"}),
    )

    async def run():
        await provider.start()

    asyncio.run(run())
    states = provider.get_server_states()
    assert states["s1"][0] is McpClientState.CONNECTED
    assert states["s2"][0] is McpClientState.FAILED


def test_mcp_connect_gate_denies_disallowed_server():
    registry = ToolRegistry()
    # 仅允许 github server 连接；evil 被 mcp.connect 网关拒绝。
    scope = default_policy_scope()
    scope.allowed_mcp_servers = ["github"]
    policy = PolicyEngine(scope)
    tool_map = {"github": [_tool("foo")], "evil": [_tool("bar")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("github"), FakeServerConfig("evil")],
        registry=registry,
        policy_engine=policy,
        client_factory=_factory(tool_map),
    )

    async def run():
        return await provider.start()

    names = asyncio.run(run())
    assert "mcp.github.foo" in names
    assert "mcp.evil.bar" not in names
    assert "evil" in provider.failed_servers
    assert "策略拒绝连接" in provider.failed_servers["evil"]


def test_disconnected_server_call_returns_mcp_unavailable():
    registry = ToolRegistry()
    tool_map = {"s1": [_tool("foo")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("s1")],
        registry=registry,
        client_factory=_factory(tool_map),
    )

    async def run():
        await provider.start()

    asyncio.run(run())
    # 模拟 server 运行中断线。
    client = provider.clients["s1"]
    client._state = McpClientState.FAILED
    adapter = registry.get("mcp.s1.foo")

    async def call():
        return await adapter.execute({})

    out = asyncio.run(call())
    assert out.error_code == "MCP_UNAVAILABLE"


def test_invalid_mcp_args_blocks_tools_call():
    # 验收：MCP 参数不合法时不发送 tools/call（开发计划阶段四）。
    from dotclaw.tools.executor import ToolExecutor

    calls = []
    client = FakeMcpClient("s1", call_result=McpToolResult(content="ok"))
    real_call = client.call_tool

    async def spy(name, args, timeout=None):
        calls.append((name, args))
        return await real_call(name, args, timeout)

    client.call_tool = spy
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    adapter = McpToolAdapter(client, name="foo", description="d", input_schema=schema)
    registry = ToolRegistry()
    registry.register(adapter)
    executor = ToolExecutor(registry)

    async def run():
        return await executor.execute("mcp.s1.foo", {"wrong": 1})

    out = asyncio.run(run())
    assert out.error_code == "INVALID_ARGUMENTS"
    assert calls == []  # tools/call 从未发送


def test_factory_pattern_awaited_discovery_populates_snapshot():
    # 回归：MCP 发现必须以可 await 任务完成（fire-and-forget 会导致首个 Run
    # 快照早于发现完成而漏掉 MCP 工具）。模拟 factory 的 create_task + await 模式。
    from dotclaw.tools.executor import ToolExecutor

    registry = ToolRegistry()
    tool_map = {"s1": [_tool("foo")]}
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[FakeServerConfig("s1")],
        registry=registry,
        client_factory=_factory(tool_map),
    )
    executor = ToolExecutor(registry)

    async def run():
        task = asyncio.create_task(provider.start())
        await task  # factory 在 Agent 启动阶段必须 await，否则首次发现未完成
        return executor.snapshot_definitions()

    snap = asyncio.run(run())
    names = [d.name for d in snap]
    assert "mcp.s1.foo" in names
