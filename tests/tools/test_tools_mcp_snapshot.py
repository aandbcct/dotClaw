"""阶段四：Run 级工具快照隔离测试（总体设计 §9 / 开发计划阶段四）。

验证：Run 创建时捕获的不可变快照，不受后续 Provider 增删工具影响；新 Run 才看到新快照。
"""

from __future__ import annotations

import asyncio

from dotclaw.mcp.client import McpToolInfo, McpClientState
from dotclaw.mcp.provider import MCPToolProvider
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.registry import ToolRegistry


class _Cfg:
    def __init__(self, name):
        self.name = name

    def get_tool_timeout(self, g=None):
        return 30.0

    def get_startup_timeout(self, g=None):
        return 10.0

    def get_restart_on_crash(self, g=None):
        return False

    def get_max_restart_attempts(self, g=None):
        return 1


class _Client:
    def __init__(self, name, tools):
        self._name = name
        self._tools = tools
        self._state = McpClientState.CONNECTED

    @property
    def server_name(self):
        return self._name

    @property
    def tools(self):
        return self._tools

    @property
    def resources(self):
        return []

    @property
    def prompts(self):
        return []

    @property
    def state(self):
        return self._state

    def get_tool_timeout(self):
        return 30.0

    def get_startup_timeout(self):
        return 10.0

    async def connect(self):
        return True

    async def call_tool(self, name, args, timeout=None):
        return None

    async def shutdown(self):
        self._state = McpClientState.SHUTDOWN


def _factory(tool_map):
    def _f(cfg, g):
        return _Client(cfg.name, tool_map.get(cfg.name, []))
    return _f


def _tool(name):
    return McpToolInfo(name=name, description="d", input_schema={})


def test_snapshot_is_immutable_tuple():
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    snap = executor.snapshot_definitions()
    assert isinstance(snap, tuple)


def test_run_a_snapshot_untouched_after_provider_change():
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[],
        registry=registry,
        client_factory=_factory({}),
    )

    # Run A 创建时捕获快照（此时无工具）。
    run_a = executor.snapshot_definitions()
    assert run_a == ()

    # Provider 在 Run A 之后新增一个 server（模拟重连/新配置）。
    provider._server_configs = [_Cfg("s1")]
    provider._client_factory = _factory({"s1": [_tool("foo")]})

    async def load():
        await provider.start()

    asyncio.run(load())

    # Run B 才看到新快照。
    run_b = executor.snapshot_definitions()
    assert "mcp.s1.foo" in [d.name for d in run_b]
    # Run A 快照不受影响（深拷贝隔离）。
    assert run_a == ()
    assert "mcp.s1.foo" not in [d.name for d in run_a]


def test_snapshot_survives_registry_unregistration():
    # 验证快照持有自身深拷贝，registry 后续清空不影响已捕获的 Run 快照。
    from dotclaw.tools.discovery import ToolDiscovery

    registry = ToolRegistry()
    for h in ToolDiscovery.discover_builtin():
        registry.register(h)
    executor = ToolExecutor(registry)

    snap = executor.snapshot_definitions()
    assert len(snap) > 0
    before = len(snap)

    # Run 之后工具集变化（清空 registry）。
    for name in registry.all_names():
        registry.unregister(name)

    # 旧快照不受影响（深拷贝隔离）。
    assert len(snap) == before
    # 新快照已反映清空。
    assert len(executor.snapshot_definitions()) == 0
