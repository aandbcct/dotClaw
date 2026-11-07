"""MCP 客户端封装（Phase 6）— 单个 MCP server 连接管理"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from contextlib import AsyncExitStack
from ..common.resource_errors import ResourceCleanupError
from enum import Enum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.types import Tool, Resource, Prompt
    from mcp.types import CallToolResult, ReadResourceResult, GetPromptResult

logger = logging.getLogger("dotclaw.mcp.client")


class McpError(Exception):
    """MCP 基础异常"""
    pass


class McpClientError(McpError):
    """MCP client 异常（连接失败等）"""
    pass


class McpUnavailableError(McpError):
    """MCP server 不可用（crashed/failed/shutdown）"""
    pass


class McpClientState(str, Enum):
    STARTING = "starting"        # 启动中
    CONNECTED = "connected"      # 已连接
    CRASHED = "crashed"          # 崩溃（重连失败次数已达上限）
    FAILED = "failed"            # 启动失败（命令不存在/握手超时）
    SHUTDOWN = "shutdown"        # 已关闭


@dataclass
class McpToolInfo:
    """MCP 工具元信息"""
    name: str
    description: str
    input_schema: dict

    @classmethod
    def from_mcp(cls, mcp_tool: "Tool") -> "McpToolInfo":
        return cls(
            name=mcp_tool.name,
            description=mcp_tool.description or "",
            input_schema=getattr(mcp_tool, 'inputSchema', {}) or {},
        )


@dataclass
class McpResourceInfo:
    """MCP 资源元信息"""
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""

    @classmethod
    def from_mcp(cls, mcp_resource: "Resource") -> "McpResourceInfo":
        return cls(
            uri=mcp_resource.uri,
            name=mcp_resource.name,
            description=getattr(mcp_resource, 'description', '') or '',
            mime_type=getattr(mcp_resource, 'mimeType', '') or '',
        )


@dataclass
class McpPromptInfo:
    """MCP 提示词元信息"""
    name: str
    description: str = ""
    arguments: list[dict] = field(default_factory=list)

    @classmethod
    def from_mcp(cls, mcp_prompt: "Prompt") -> "McpPromptInfo":
        return cls(
            name=mcp_prompt.name,
            description=getattr(mcp_prompt, 'description', '') or '',
            arguments=[
                {
                    "name": a.name,
                    "description": getattr(a, 'description', ''),
                    "required": getattr(a, 'required', False),
                    "type": getattr(a, 'type', 'string'),
                }
                for a in getattr(mcp_prompt, 'arguments', []) or []
            ],
        )


@dataclass
class McpToolResult:
    """MCP 调用结果（统一）"""
    content: str
    is_error: bool = False
    error_message: str = ""

    @classmethod
    def from_mcp(cls, mcp_result: "CallToolResult") -> "McpToolResult":
        """从 tools/call 结果构建"""
        text_parts = []
        for item in mcp_result.content:
            if hasattr(item, 'text'):
                text_parts.append(item.text)
            elif hasattr(item, 'data') and hasattr(item, 'mimeType'):
                mime = getattr(item, 'mimeType', 'unknown')
                text_parts.append(f"[{mime} content, {len(item.data)} bytes]")
            else:
                text_parts.append(f"[unsupported content type: {type(item).__name__}]")
        return cls(
            content="\n".join(text_parts),
            is_error=getattr(mcp_result, 'isError', False),
        )

    @classmethod
    def from_resource_result(cls, mcp_result: "ReadResourceResult") -> "McpToolResult":
        """从 resources/read 结果构建"""
        text_parts = []
        for item in mcp_result.contents:
            if hasattr(item, 'text'):
                text_parts.append(item.text)
            elif hasattr(item, 'blob'):
                mime = getattr(item, 'mimeType', 'unknown')
                text_parts.append(f"[{mime} content, {len(item.blob)} bytes]")
            else:
                text_parts.append(f"[unsupported resource type: {type(item).__name__}]")
        return cls(content="\n".join(text_parts))

    @classmethod
    def from_prompt_result(cls, mcp_result: "GetPromptResult") -> "McpToolResult":
        """从 prompts/get 结果构建"""
        text_parts = []
        for item in mcp_result.messages:
            if hasattr(item, 'content') and hasattr(item.content, 'text'):
                text_parts.append(f"[{item.role}] {item.content.text}")
            else:
                text_parts.append(f"[{getattr(item, 'role', 'unknown')}] {item}")
        return cls(content="\n".join(text_parts))


class McpClient:
    """单个 MCP server 客户端封装"""

    def __init__(self, server_config, global_config):
        """_config / _global 类型为 McpServerConfig / McpGlobalConfig（from settings）"""
        self._config = server_config
        self._global = global_config
        self._session = None          # mcp.ClientSession
        self._transport = None        # 仅在连接保活任务中拥有的传输流对。
        self._connection_task: asyncio.Task[None] | None = None
        self._connection_stop: asyncio.Event | None = None
        self._connection_ready: asyncio.Future[bool] | None = None
        self._connection_lock = asyncio.Lock()
        self._state = McpClientState.STARTING
        self._failure_count = 0
        self._tools: list[McpToolInfo] = []
        self._resources: list[McpResourceInfo] = []
        self._prompts: list[McpPromptInfo] = []

    @property
    def state(self) -> McpClientState:
        return self._state

    @property
    def server_name(self) -> str:
        return self._config.name

    @property
    def tools(self) -> list[McpToolInfo]:
        return self._tools

    @property
    def resources(self) -> list[McpResourceInfo]:
        return self._resources

    @property
    def prompts(self) -> list[McpPromptInfo]:
        return self._prompts

    # ---- 公开配置访问 ----

    def get_tool_timeout(self) -> float:
        return self._config.get_tool_timeout(self._global)

    def get_startup_timeout(self) -> float:
        return self._config.get_startup_timeout(self._global)

    # ---- 连接管理 ----

    async def connect(self) -> bool:
        """按已安装 SDK 的上下文协议连接，连接资源始终由同一任务进入和退出。"""
        async with self._connection_lock:
            await self._cleanup_old_connection()
            self._state = McpClientState.STARTING
            self._connection_stop = asyncio.Event()
            self._connection_ready = asyncio.get_running_loop().create_future()
            self._connection_task = asyncio.create_task(self._serve_connection())
            try:
                return await asyncio.wait_for(asyncio.shield(self._connection_ready), self.get_startup_timeout())
            except asyncio.CancelledError:
                await self._cleanup_old_connection()
                raise
            except Exception as error:
                await self._cleanup_old_connection()
                self._state = McpClientState.FAILED
                logger.error("MCP server %s 启动失败: %s", self._config.name, type(error).__name__)
                return False

    async def _serve_connection(self) -> None:
        """共享连接保活；Run 取消只打断 request await，不取消此任务。"""
        try:
            from mcp import ClientSession
            async with AsyncExitStack() as stack:
                if self._config.transport == "stdio":
                    from mcp.client.stdio import StdioServerParameters, stdio_client
                    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                        command=self._config.command, args=self._config.args)))
                else:
                    from mcp.client.streamable_http import streamable_http_client
                    from mcp.shared._httpx_utils import create_mcp_http_client
                    http_client = await stack.enter_async_context(create_mcp_http_client(headers=self._config.headers))
                    streams = await stack.enter_async_context(streamable_http_client(self._config.url, http_client=http_client))
                self._transport = streams
                self._session = await stack.enter_async_context(ClientSession(*streams))
                await self._session.initialize()
                await self._discover()
                self._state = McpClientState.CONNECTED
                self._failure_count = 0
                self._connection_ready.set_result(True)
                await self._connection_stop.wait()
        except BaseException as error:
            if not self._connection_ready.done():
                self._connection_ready.set_exception(error)
            elif not isinstance(error, asyncio.CancelledError):
                self._state = McpClientState.CRASHED
                logger.error("MCP server %s 连接退出: %s", self._config.name, type(error).__name__)
                raise ResourceCleanupError("mcp_connection", "mcp_connection_close_failed", "MCP连接清理失败") from error
        finally:
            self._session = None
            self._transport = None
            if self._connection_stop is not None and not self._connection_stop.is_set():
                self._state = McpClientState.CRASHED

    async def _discover(self):
        """发现 tools/resources/prompts"""
        tools_result = await self._session.list_tools()
        self._tools = [McpToolInfo.from_mcp(t) for t in tools_result.tools]

        try:
            resources_result = await self._session.list_resources()
            self._resources = [McpResourceInfo.from_mcp(r) for r in resources_result.resources]
        except Exception:
            self._resources = []

        try:
            prompts_result = await self._session.list_prompts()
            self._prompts = [McpPromptInfo.from_mcp(p) for p in prompts_result.prompts]
        except Exception:
            self._prompts = []

    # ---- 工具执行 ----

    async def call_tool(
        self, tool_name: str, arguments: dict, timeout: float | None = None
    ) -> McpToolResult:
        """调用工具（MCP tools/call）"""
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._config.name} 不可用（state={self._state}）")

        timeout_val = timeout or self.get_tool_timeout()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=timeout_val,
            )
            return McpToolResult.from_mcp(result)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # SDK 2.0 根据当前请求 ID 发送取消通知并移除相应等待者；绝不猜测整连接取消。
            raise
        except ResourceCleanupError:
            raise
        except Exception as e:
            await self._handle_execution_error(e)
            raise

    async def read_resource(self, uri: str, timeout: float | None = None) -> McpToolResult:
        """读取 MCP resource（resources/read）— M1 修复：添加 timeout 参数"""
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._config.name} 不可用（state={self._state}）")
        timeout_val = timeout or self.get_tool_timeout()
        try:
            result = await asyncio.wait_for(
                self._session.read_resource(uri),
                timeout=timeout_val,
            )
            return McpToolResult.from_resource_result(result)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # SDK 2.0 根据当前请求 ID 发送取消通知并移除相应等待者；绝不猜测整连接取消。
            raise
        except ResourceCleanupError:
            raise
        except Exception as e:
            await self._handle_execution_error(e)
            raise

    async def get_prompt(self, name: str, arguments: dict, timeout: float | None = None) -> McpToolResult:
        """获取 MCP prompt（prompts/get）— M1 修复：添加 timeout 参数"""
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._config.name} 不可用（state={self._state}）")
        timeout_val = timeout or self.get_tool_timeout()
        try:
            result = await asyncio.wait_for(
                self._session.get_prompt(name, arguments),
                timeout=timeout_val,
            )
            return McpToolResult.from_prompt_result(result)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # SDK 2.0 根据当前请求 ID 发送取消通知并移除相应等待者；绝不猜测整连接取消。
            raise
        except ResourceCleanupError:
            raise
        except Exception as e:
            await self._handle_execution_error(e)
            raise

    # ---- 重连与关闭 ----

    async def _handle_execution_error(self, error: Exception):
        """处理执行错误：重连逻辑"""
        if not self._config.get_restart_on_crash(self._global):
            self._state = McpClientState.CRASHED
            return

        self._failure_count += 1
        max_attempts = self._config.get_max_restart_attempts(self._global)
        if self._failure_count >= max_attempts:
            self._state = McpClientState.CRASHED
            logger.error(f"MCP server {self._config.name} 重连失败次数已达上限")
            return

        logger.warning(f"MCP server {self._config.name} 尝试重连（{self._failure_count}/{max_attempts}）")
        success = await self.connect()
        if not success:
            self._state = McpClientState.CRASHED

    async def shutdown(self):
        """只在宿主关闭时退出共享连接；重复调用幂等。"""
        async with self._connection_lock:
            await self._cleanup_old_connection()
            self._state = McpClientState.SHUTDOWN

    async def _cleanup_old_connection(self):
        """等待保活任务在自身上下文关闭 SDK 和传输，不跨任务退出取消作用域。"""
        task = self._connection_task
        if task is None:
            return
        if self._connection_stop is not None:
            self._connection_stop.set()
        if self._connection_ready is not None and not self._connection_ready.done() and not task.done():
            task.cancel()
        interrupted = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    break
                interrupted = True
        if self._connection_ready is not None and self._connection_ready.done() and not self._connection_ready.cancelled():
            self._connection_ready.exception()
        self._connection_task = None
        self._connection_stop = None
        self._connection_ready = None
        if interrupted:
            raise asyncio.CancelledError()
