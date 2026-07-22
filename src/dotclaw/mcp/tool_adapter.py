"""MCP 工具适配层（Tool v1 阶段四重构）— 仅 MCP tools → ToolHandler。

职责边界（总体设计 §6 / 开发计划阶段四）：
- 只把 MCP `tools/list` 的工具适配为 ToolHandler，注册名统一为 `mcp.<server>.<tool>`，
  并保留原始 MCP tool 名用于实际 `tools/call`（协议调用仍使用原始名）。
- resources / prompts 不再伪装为工具：其原生读取入口保留在 McpClient（read_resource /
  get_prompt），但不进入 Tool Registry，也不在此注册。
- 本适配层只做协议参数/结果转换与超时/不可用归一化；不负责注册表生命周期、
  Policy 决策或连接管理。

所有新增注释使用中文。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from dotclaw.tools.base import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolSource,
)
from dotclaw.tools.handler import ToolHandler
from dotclaw.tools.decorator import ToolPolicy
from .client import McpClient, McpUnavailableError

logger = logging.getLogger("dotclaw.mcp.adapter")


_INVALID_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_identifier(value: str) -> str:
    """将任意字符串规范化为合法标识符片段。

    非 `[A-Za-z0-9_]` 的字符统一替换为 `_`，空输入回退为单个 `_`，
    保证注册名始终为合法、非空片段（满足设计"规范化为合法标识符"要求）。
    """
    cleaned = _INVALID_IDENT_RE.sub("_", value)
    return cleaned or "_"


def mcp_tool_name(server: str, original_tool: str) -> str:
    """构造 MCP 工具的命名空间注册名（总体设计 §4.2 / §4.4）。

    保留原始 MCP tool 名的可识别形式，前缀 `mcp.<server>.` 保证不同 server 的
    同名工具互不冲突。server 与原始 tool 名均规范化为合法标识符片段，避免
    含空格/点/斜杠等字符造成注册名非法或命名空间歧义。实际协议调用仍使用
    原始名（由 McpToolAdapter 单独保存），不受影响。
    """
    return f"mcp.{_sanitize_identifier(server)}.{_sanitize_identifier(original_tool)}"


class McpToolAdapter(ToolHandler):
    """MCP tool 调用执行器（tools/call）。

    注册名带 server 命名空间；保存原始 MCP tool 名用于协议调用；将 MCP 结果
    转换为统一 ToolResult，并对超时/不可用/协议错误归一化为标准错误码。
    """

    def __init__(
        self,
        client: McpClient,
        name: str,
        description: str,
        input_schema: dict,
        timeout: float = 60.0,
        needs_approval: bool = False,
    ):
        # name 此处为原始 MCP tool 名；注册名由工具提供者统一加命名空间。
        self._client = client
        self._mcp_tool_name = name
        self._server = client.server_name
        self._description = description
        self._input_schema = input_schema or {}
        self._timeout = timeout
        self._needs_approval = needs_approval
        self._definition = ToolDefinition(
            name=mcp_tool_name(self._server, name),
            description=description,
            parameters=self._input_schema,
            source=ToolSource.MCP,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata={
                "server": self._server,
                "mcp_type": "tool",
                "mcp_tool_name": name,
            },
            # 声明 MCP 档案，执行链据此形成 mcp.call 资源请求（总体设计 §4.3）。
            policy_profile=ToolPolicy.MCP.value,
        )

    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def input_schema(self) -> dict:
        # 覆盖 ToolHandler 默认 None，供执行器走 JSON Schema 校验分支。
        return self._input_schema

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # 超时设计：ToolExecutor 在外部用 asyncio.wait_for 控制超时；
        # 此处通过 client.call_tool(timeout=...) 设置客户端超时作为兜底保护。
        timeout = self._timeout
        if context is not None and context.timeout:
            timeout = context.timeout
        try:
            result = await self._client.call_tool(
                self._mcp_tool_name, arguments, timeout=timeout
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
                error_type="mcp_unavailable",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 工具执行错误: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )
