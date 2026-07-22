"""MCP 协议集成模块（Tool v1 阶段四）。

导出范围（总体设计 §4.4）：仅 tools 适配与 Provider 进入 Tool Registry；
resources / prompts 的原生读取入口（McpClient.read_resource / get_prompt）
不在此导出为工具。
"""

from .client import (
    McpClient,
    McpClientState,
    McpError,
    McpClientError,
    McpUnavailableError,
    McpToolInfo,
    McpResourceInfo,
    McpPromptInfo,
    McpToolResult,
)
from .tool_adapter import McpToolAdapter, mcp_tool_name
from .provider import MCPToolProvider

__all__ = [
    "McpClient",
    "McpClientState",
    "McpError",
    "McpClientError",
    "McpUnavailableError",
    "McpToolInfo",
    "McpResourceInfo",
    "McpPromptInfo",
    "McpToolResult",
    "McpToolAdapter",
    "mcp_tool_name",
    "MCPToolProvider",
]
