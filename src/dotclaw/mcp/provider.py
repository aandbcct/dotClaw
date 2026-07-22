"""MCP 工具提供者（Tool v1 阶段四重构）— ToolProvider ABC 实现。

职责边界（总体设计 §4.4 / §6，开发计划阶段四）：
- 只发现并注册 MCP tools（命名空间名 `mcp.<server>.<tool>`）；resources / prompts
  不再注册为工具（其原生入口保留在 McpClient，不进入 Tool Registry）。
- 连接前先用 Policy 评估 `mcp.connect` 网关：被拒绝的 server 标记为失败状态、
  不阻塞 Agent 启动、也不会发起网络连接的副作用。
- 记录 server 成功/失败状态，供 CLI / 可观测性读取；单个 server 失败可降级。
- 支持 client_factory 注入，便于在无真实 server 的环境下测试。

所有新增注释使用中文。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from dotclaw.tools.registry import ToolRegistry
from dotclaw.tools.provider import ToolProvider
from dotclaw.tools.policy import PolicyDecision
from dotclaw.tools.capability import CapabilityRequest, ResourceKind
from .client import McpClient, McpClientState, McpClientError
from .tool_adapter import McpToolAdapter

logger = logging.getLogger("dotclaw.mcp.provider")

# client_factory: 给定 (server_config, global_config) 返回 McpClient 实例。
ClientFactory = Callable[[Any, Any], McpClient]


class MCPToolProvider(ToolProvider):
    """
    MCP 工具提供者（ToolProvider ABC 实现）。

    编排：遍历 mcp_servers 配置 → 创建 clients → 策略网关 → 连接 → 注册 tools。
    生命周期：start / shutdown / get_server_states。
    状态：clients + failed_servers + pending_servers。
    """

    def __init__(
        self,
        global_config,
        server_configs: list,
        registry: ToolRegistry,
        policy_engine: Any | None = None,
        capability_broker: Any | None = None,
        client_factory: ClientFactory | None = None,
    ):
        self._global = global_config
        self._server_configs = server_configs
        self._registry = registry
        self._policy_engine = policy_engine
        self._capability_broker = capability_broker
        self._client_factory = client_factory

        self._clients: dict[str, McpClient] = {}
        self._failed_servers: dict[str, str] = {}
        self._pending_servers: dict[str, str] = {}
        self._started = False

    @property
    def clients(self) -> dict[str, McpClient]:
        return self._clients

    @property
    def failed_servers(self) -> dict[str, str]:
        return self._failed_servers

    async def discover_and_register(self, registry: ToolRegistry) -> list[str]:
        """ToolProvider ABC 接口实现。"""
        return await self.start()

    async def start(self) -> list[str]:
        """启动所有 MCP servers（并行连接并注册工具）。"""
        if self._started:
            logger.warning("MCPToolProvider 已启动，忽略重复调用")
            return []
        self._started = True
        registered_tools: list[str] = []

        for cfg in self._server_configs:
            self._pending_servers[cfg.name] = "connecting"

        clients = [self._make_client(cfg) for cfg in self._server_configs]
        results = await asyncio.gather(
            *[self._connect_and_register(client) for client in clients],
            return_exceptions=True,
        )

        for cfg, result in zip(self._server_configs, results):
            if isinstance(result, Exception):
                self._failed_servers[cfg.name] = str(result)
                self._pending_servers.pop(cfg.name, None)
                logger.error(f"MCP server {cfg.name} 启动失败: {result}")
            else:
                registered_tools.extend(result)

        logger.info(f"MCP 已加载 {len(registered_tools)} 个工具")
        return registered_tools

    def _make_client(self, cfg) -> McpClient:
        """构造单个 MCP client（支持注入 client_factory 以便测试）。"""
        if self._client_factory is not None:
            return self._client_factory(cfg, self._global)
        return McpClient(cfg, self._global)

    async def _connect_and_register(self, client: McpClient) -> list[str]:
        """策略网关 → 连接 → 仅注册 tools（命名空间名）。"""
        server = client.server_name

        # ① mcp.connect 策略网关：DENY → 标记失败，不阻塞 Agent、不发起网络副作用。
        deny_reason = self._authorize_connect(server)
        if deny_reason is not None:
            self._failed_servers[server] = deny_reason
            self._pending_servers.pop(server, None)
            logger.warning(f"MCP server {server} 连接被策略拒绝: {deny_reason}")
            return []

        success = await client.connect()
        if not success:
            raise McpClientError(f"连接失败: {client.server_name}")

        self._pending_servers.pop(server, None)

        # ② 仅注册 tools，使用命名空间名 mcp.<server>.<tool>。
        registered: list[str] = []
        needs_approval = self._mcp_needs_approval()
        for tool_info in client.tools:
            handler = McpToolAdapter(
                client=client,
                name=tool_info.name,
                description=tool_info.description,
                input_schema=tool_info.input_schema,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(handler.definition().name)

        # 注册成功后才加入 clients（防止异常时状态不一致）。
        self._clients[server] = client
        return registered

    def _authorize_connect(self, server: str) -> str | None:
        """评估 `mcp.connect` 网关。返回拒绝原因字符串（非 None）表示禁止连接。

        无 Policy 引擎时放行（降级为不网关校验）。
        """
        if self._policy_engine is None:
            return None
        request = CapabilityRequest(
            kind=ResourceKind.MCP_CONNECT,
            profile="mcp.connect",
            server=server,
        )
        outcome = self._policy_engine.evaluate([request])
        if outcome.decision is PolicyDecision.DENY:
            return f"策略拒绝连接（{outcome.reason}）"
        return None

    def _mcp_needs_approval(self) -> bool:
        """由全局规则 mcp.call 推导是否需要审批（ask → 需要）。"""
        if self._policy_engine is None or self._policy_engine.scope is None:
            return False
        return (
            self._policy_engine.scope.global_rules.get("mcp.call")
            is PolicyDecision.ASK
        )

    async def shutdown(self):
        """关闭所有 MCP servers。"""
        for client in self._clients.values():
            await client.shutdown()
        self._clients.clear()
        self._pending_servers.clear()
        self._started = False

    def get_server_states(self) -> dict[str, tuple[McpClientState, str]]:
        """获取所有 servers 的状态（含 pending）。"""
        result: dict[str, tuple[McpClientState, str]] = {}

        for name, status in self._pending_servers.items():
            result[name] = (McpClientState.STARTING, status)

        for name, client in self._clients.items():
            result[name] = (client.state, "")

        for name, reason in self._failed_servers.items():
            result[name] = (McpClientState.FAILED, reason)

        return result
