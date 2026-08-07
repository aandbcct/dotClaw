"""PR5 固定有限安全决策表与无副作用装配。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from dotclaw.tools.base import ToolDefinition, ToolExecutionContext, ToolResult, ToolSource
from dotclaw.tools.decorator import ToolPolicy, get_tool_meta, tool
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.handler import ToolHandler
from dotclaw.tools.registry import ToolRegistry


MATRIX_SCHEMA_VERSION = "1.0"
SUITE_CAPABILITY = "reliability_capability_v1"
SENSITIVE_MARKER = "PR5_SECRET"
_KNOWN_TOOL_NAMES: frozenset[str] = frozenset({
    "cap.file.read", "cap.file.write", "cap.process.exec", "cap.network.tavily",
    "cap.network.bad_host", "cap.mcp.github", "cap.mcp.evil",
})


@dataclass(frozen=True)
class CapabilityMatrixCase:
    """一行经审核的安全决策输入与预期结果。"""

    case_id: str
    tool: str
    arguments: Mapping[str, object]
    expected: str
    approval: str = "none"
    pre_approved: bool = False
    global_rules: Mapping[str, str] | None = None
    agent_id: str = ""
    agent_rules: Mapping[str, Mapping[str, str]] | None = None
    network_services: Mapping[str, list[str]] | None = None
    allowed_mcp_servers: list[str] | None = None
    windows_junction: bool = False


def load_matrix(path: Path) -> tuple[CapabilityMatrixCase, ...]:
    """严格读取 Git 跟踪的安全矩阵；错误类型和重复标识立即失败。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取安全矩阵：{error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != MATRIX_SCHEMA_VERSION:
        raise ValueError("安全矩阵 schema_version 不受支持")
    rows = payload.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("安全矩阵 cases 必须是非空数组")
    cases: list[CapabilityMatrixCase] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"矩阵第 {index} 行必须是对象")
        case_id, tool, expected = row.get("case_id"), row.get("tool"), row.get("expected")
        arguments = row.get("arguments")
        if not all(isinstance(value, str) and value for value in (case_id, tool, expected)):
            raise ValueError(f"矩阵第 {index} 行缺少有效 case_id/tool/expected")
        if case_id in seen or not isinstance(arguments, dict):
            raise ValueError(f"矩阵第 {index} 行 case_id 重复或 arguments 非对象")
        if tool not in _KNOWN_TOOL_NAMES:
            raise ValueError(f"矩阵第 {index} 行引用未知工具 {tool!r}")
        if expected not in {"ALLOW", "INVALID_ARGUMENTS", "POLICY_DENIED", "APPROVAL_DENIED"}:
            raise ValueError(f"矩阵第 {index} 行 expected 不受支持")
        approval = row.get("approval", "none")
        if approval not in {"none", "approve", "reject"} or not isinstance(row.get("pre_approved", False), bool):
            raise ValueError(f"矩阵第 {index} 行审批字段不合法")
        seen.add(case_id)
        cases.append(CapabilityMatrixCase(
            case_id=case_id, tool=tool, arguments=arguments, expected=expected, approval=approval,
            pre_approved=row.get("pre_approved", False), global_rules=row.get("global_rules"),
            agent_id=row.get("agent_id", ""), agent_rules=row.get("agent_rules"),
            network_services=row.get("network_services"), allowed_mcp_servers=row.get("allowed_mcp_servers"),
            windows_junction=row.get("windows_junction", False),
        ))
    return tuple(cases)


class _PathArgs(BaseModel):
    path: str


class _CommandArgs(BaseModel):
    command: str


class _NetworkArgs(BaseModel):
    url: str = ""


class _McpHandler(ToolHandler):
    """最小 MCP Handler（处理器）：仅提供静态 server 元数据，无真实连接。"""

    def __init__(self, name: str, server: str) -> None:
        self._definition = ToolDefinition(name=name, description="记录型 MCP", source=ToolSource.MCP, metadata={"server": server})

    @property
    def name(self) -> str:
        return self._definition.name

    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def input_schema(self) -> dict:
        """MCP 调用必须带结构化 operation，供参数校验屏障覆盖。"""
        return {
            "type": "object",
            "properties": {"operation": {"type": "string"}},
            "required": ["operation"],
        }

    async def execute(self, arguments: Any, context: ToolExecutionContext | None = None) -> ToolResult:
        return ToolResult(output="mcp-recorded")


@tool(name="cap.file.read", description="记录型读文件", policy=ToolPolicy.WORKSPACE_READ, args_model=_PathArgs)
async def _read(args: _PathArgs, context: ToolExecutionContext) -> str:
    return args.path


@tool(name="cap.file.write", description="记录型写文件", policy=ToolPolicy.WORKSPACE_WRITE, args_model=_PathArgs)
async def _write(args: _PathArgs, context: ToolExecutionContext) -> str:
    return args.path


@tool(name="cap.process.exec", description="记录型进程", policy=ToolPolicy.PROCESS, args_model=_CommandArgs)
async def _process(args: _CommandArgs, context: ToolExecutionContext) -> str:
    return args.command


@tool(name="cap.network.tavily", description="记录型网络", policy=ToolPolicy.NETWORK, args_model=_NetworkArgs, network_service="tavily", network_hosts=["api.tavily.com"])
async def _network(args: _NetworkArgs, context: ToolExecutionContext) -> str:
    return "network-recorded"


@tool(name="cap.network.bad_host", description="错误静态主机", policy=ToolPolicy.NETWORK, args_model=_NetworkArgs, network_service="tavily", network_hosts=["evil.invalid"])
async def _network_bad_host(args: _NetworkArgs, context: ToolExecutionContext) -> str:
    return "network-recorded"


def build_registry() -> ToolRegistry:
    """构造固定无副作用工具集，禁止真实文件、进程、网络或 MCP 调用。"""
    registry = ToolRegistry()
    for func in (_read, _write, _process, _network, _network_bad_host):
        registry.register(FunctionToolHandler(func, get_tool_meta(func)))
    registry.register(_McpHandler("cap.mcp.github", "github"))
    registry.register(_McpHandler("cap.mcp.evil", "evil"))
    return registry
