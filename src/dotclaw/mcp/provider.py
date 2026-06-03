"""MCP 工具提供者（Phase 6）— ToolProvider ABC 实现"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dotclaw.tools.registry import ToolRegistry
from dotclaw.tools.provider import ToolProvider
from .client import McpClient, McpClientState, McpClientError
from .tool_adapter import McpToolCallHandler, McpResourceHandler, McpPromptHandler

logger = logging.getLogger("dotclaw.mcp.provider")


class MCPToolProvider(ToolProvider):
    """
    MCP 工具提供者（ToolProvider ABC 实现）。

    职责：
    - 编排：遍历 mcp_servers 配置 → 创建 clients → 注册 tools
    - 生命周期：start / shutdown / get_server_states
    - 状态管理：clients + failed_servers + pending_servers
    - 后台加载：可配合 asyncio.create_task
    """

    def __init__(
        self,
        global_config,       # McpGlobalConfig
        server_configs: list, # list[McpServerConfig]
        registry: ToolRegistry,
        approval_commands: list[str] | None = None,
    ):
        self._global = global_config
        self._server_configs = server_configs
        self._registry = registry
        self._approval_commands = set(approval_commands) if approval_commands else set()

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
        """ToolProvider ABC 接口实现"""
        return await self.start()

    async def start(self) -> list[str]:
        """启动所有 MCP servers（并行连接并注册工具）"""
        if self._started:
            logger.warning("MCPToolProvider 已启动，忽略重复调用")
            return []

        self._started = True
        registered_tools: list[str] = []

        for cfg in self._server_configs:
            self._pending_servers[cfg.name] = "connecting"

        tasks = []
        for cfg in self._server_configs:
            client = McpClient(cfg, self._global)
            tasks.append(self._connect_and_register(client))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for cfg, result in zip(self._server_configs, results):
            if isinstance(result, Exception):
                self._failed_servers[cfg.name] = str(result)
                self._pending_servers.pop(cfg.name, None)
                logger.error(f"MCP server {cfg.name} 启动失败: {result}")
            else:
                registered_tools.extend(result)

        logger.info(f"MCP 已加载 {len(registered_tools)} 个工具")
        return registered_tools

    async def _connect_and_register(self, client: McpClient) -> list[str]:
        """连接单个 MCP server 并注册工具"""
        success = await client.connect()
        if not success:
            raise McpClientError(f"连接失败: {client.server_name}")

        self._pending_servers.pop(client.server_name, None)
        registered: list[str] = []

        # 注册 tools
        for tool_info in client.tools:
            needs_approval = tool_info.name in self._approval_commands
            handler = McpToolCallHandler(
                client=client,
                name=tool_info.name,
                description=tool_info.description,
                input_schema=tool_info.input_schema,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(tool_info.name)

        # 注册 resources
        for resource_info in client.resources:
            resource_tool_name = f"read_{client.server_name}_{resource_info.name}"
            needs_approval = resource_tool_name in self._approval_commands
            handler = McpResourceHandler(
                client=client,
                resource_name=resource_info.name,
                uri=resource_info.uri,
                description=resource_info.description,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(handler.definition().name)

        # 注册 prompts
        for prompt_info in client.prompts:
            prompt_tool_name = f"prompt_{client.server_name}_{prompt_info.name}"
            needs_approval = prompt_tool_name in self._approval_commands
            handler = McpPromptHandler(
                client=client,
                prompt_name=prompt_info.name,
                description=prompt_info.description,
                arguments_info=prompt_info.arguments,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(handler.definition().name)

        # W2 修复：全部注册成功后才加入 clients（防止异常时状态不一致）
        self._clients[client.server_name] = client
        return registered

    async def shutdown(self):
        """关闭所有 MCP servers"""
        for client in self._clients.values():
            await client.shutdown()
        self._clients.clear()
        self._pending_servers.clear()
        self._started = False

    def get_server_states(self) -> dict[str, tuple[McpClientState, str]]:
        """获取所有 servers 的状态（含 pending）"""
        result = {}

        for name, status in self._pending_servers.items():
            result[name] = (McpClientState.STARTING, status)

        for name, client in self._clients.items():
            result[name] = (client.state, "")

        for name, reason in self._failed_servers.items():
            result[name] = (McpClientState.FAILED, reason)

        return result
