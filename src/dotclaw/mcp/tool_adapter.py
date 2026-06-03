"""MCP 工具适配层（Phase 6）— MCP tools/resources/prompts → ToolHandler"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dotclaw.tools.base import ToolDefinition, ToolExecutionContext, ToolResult, ToolSource
from dotclaw.tools.handler import ToolHandler
from .client import McpClient, McpUnavailableError

logger = logging.getLogger("dotclaw.mcp.adapter")


class McpToolCallHandler(ToolHandler):
    """MCP tool 调用执行器（tools/call）"""

    def __init__(
        self,
        client: McpClient,
        name: str,
        description: str,
        input_schema: dict,
        timeout: float = 60.0,
        needs_approval: bool = False,
    ):
        self._client = client
        self._name = name
        self._description = description
        self._input_schema = input_schema
        self._timeout = timeout
        self._needs_approval = needs_approval
        self._definition = ToolDefinition(
            name=name,
            description=description,
            parameters=input_schema,
            source=ToolSource.MCP,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata={
                "server": client.server_name,
                "mcp_type": "tool",
            },
        )

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # M4 修复：使用 context.timeout 作为 fallback
        timeout = self._timeout
        if context is not None and context.timeout:
            timeout = context.timeout
        # 超时设计：ToolExecutor 在外部用 asyncio.wait_for 控制超时。
        # Handler 内部通过 client.call_tool(timeout=...) 设置客户端超时作为兜底保护。
        try:
            result = await self._client.call_tool(
                self._name, arguments, timeout=timeout
            )
            return ToolResult(
                output=result.content,
                is_error=result.is_error,
                error_code="EXECUTION_ERROR" if result.is_error else None,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"MCP 工具执行超时（{timeout}s）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
        except McpUnavailableError as e:
            return ToolResult(
                output=f"MCP server 不可用: {e}",
                is_error=True,
                error_code="MCP_UNAVAILABLE",
                error_type="mcp",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 工具执行错误: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )


class McpResourceHandler(ToolHandler):
    """MCP resource 读取执行器（resources/read）"""

    def __init__(
        self,
        client: McpClient,
        resource_name: str,
        uri: str,
        description: str = "",
        timeout: float = 60.0,
        needs_approval: bool = False,
    ):
        self._client = client
        self._uri = uri
        self._timeout = timeout
        self._needs_approval = needs_approval
        server = client.server_name
        tool_name = f"read_{server}_{resource_name}"
        self._definition = ToolDefinition(
            name=tool_name,
            description=f"读取资源: {description or resource_name}",
            parameters={"type": "object", "properties": {}},
            source=ToolSource.MCP,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata={
                "server": server,
                "mcp_type": "resource",
                "uri": uri,
            },
        )

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # M4 修复：context.timeout fallback
        timeout = self._timeout
        if context is not None and context.timeout:
            timeout = context.timeout
        try:
            result = await self._client.read_resource(self._uri, timeout=timeout)
            return ToolResult(output=result.content)
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"MCP 资源读取超时（{timeout}s）",
                is_error=True, error_code="TIMEOUT", error_type="timeout",
            )
        except McpUnavailableError as e:
            return ToolResult(
                output=f"MCP server 不可用: {e}",
                is_error=True, error_code="MCP_UNAVAILABLE", error_type="mcp",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 资源读取错误: {e}",
                is_error=True, error_code="EXECUTION_ERROR", error_type="execution",
            )


class McpPromptHandler(ToolHandler):
    """MCP prompt 获取执行器（prompts/get）"""

    def __init__(
        self,
        client: McpClient,
        prompt_name: str,
        description: str = "",
        arguments_info: list[dict] | None = None,
        timeout: float = 60.0,
        needs_approval: bool = False,
    ):
        self._client = client
        self._prompt_name = prompt_name
        self._timeout = timeout
        self._needs_approval = needs_approval

        server = client.server_name
        tool_name = f"prompt_{server}_{prompt_name}"

        # 构建参数 schema
        properties = {}
        required_fields = []
        for arg in (arguments_info or []):
            properties[arg["name"]] = {
                "type": arg.get("type", "string"),
                "description": arg.get("description", ""),
            }
            if arg.get("required", False):
                required_fields.append(arg["name"])

        parameters = {"type": "object", "properties": properties}
        if required_fields:
            parameters["required"] = required_fields

        self._definition = ToolDefinition(
            name=tool_name,
            description=f"获取提示词模板: {description or prompt_name}",
            parameters=parameters,
            source=ToolSource.MCP,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata={
                "server": server,
                "mcp_type": "prompt",
                "prompt_name": prompt_name,
            },
        )

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # M4 修复：context.timeout fallback
        timeout = self._timeout
        if context is not None and context.timeout:
            timeout = context.timeout
        try:
            result = await self._client.get_prompt(self._prompt_name, arguments, timeout=timeout)
            return ToolResult(output=result.content)
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"MCP 提示词获取超时（{timeout}s）",
                is_error=True, error_code="TIMEOUT", error_type="timeout",
            )
        except McpUnavailableError as e:
            return ToolResult(
                output=f"MCP server 不可用: {e}",
                is_error=True, error_code="MCP_UNAVAILABLE", error_type="mcp",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 提示词获取错误: {e}",
                is_error=True, error_code="EXECUTION_ERROR", error_type="execution",
            )
