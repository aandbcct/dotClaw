"""工具模块（Tool v1 阶段一）— 导出新架构。

新增契约符号与处理函数；旧 BuiltinToolHandler / register_all 仍保留，
待阶段二迁移完成后删除（不在此阶段移除）。
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
from .handler import ToolHandler, BuiltinToolHandler
from .registry import ToolRegistry, DuplicateToolError
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
    "BuiltinToolHandler",
    "ToolRegistry",
    "DuplicateToolError",
    "ToolExecutor",
    "ApprovalManager",
    "ToolProvider",
]
