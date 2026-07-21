"""工具模块（Tool v1 阶段二）— 导出新架构。

旧 BuiltinToolHandler / register_all / get_*_handler 已在阶段二迁移中删除；
新架构仅依赖 @tool 声明 + ToolDiscovery 自动发现。所有新增注释使用中文。
"""

from .base import (
    ToolSource,
    ToolDefinition,
    ToolResult,
    ToolExecutionContext,
    ToolContext,
    ToolErrorCode,
    ToolErrorType,
)
from .decorator import ToolPolicy, ToolMeta, tool, get_tool_meta
from .schema import to_json_schema, validate_args, ToolValidationError
from .function_handler import FunctionToolHandler
from .handler import ToolHandler
from .registry import ToolRegistry, DuplicateToolError
from .discovery import ToolDiscovery, ToolDeclarationError
from .executor import ToolExecutor
from .approval import ApprovalManager
from .provider import ToolProvider

__all__ = [
    "ToolSource",
    "ToolDefinition",
    "ToolResult",
    "ToolExecutionContext",
    "ToolContext",
    "ToolErrorCode",
    "ToolErrorType",
    "ToolPolicy",
    "ToolMeta",
    "tool",
    "get_tool_meta",
    "to_json_schema",
    "validate_args",
    "ToolValidationError",
    "FunctionToolHandler",
    "ToolHandler",
    "ToolRegistry",
    "DuplicateToolError",
    "ToolDiscovery",
    "ToolDeclarationError",
    "ToolExecutor",
    "ApprovalManager",
    "ToolProvider",
]
