"""工具模块（Phase 5 重构）— 导出新架构"""

from .base import ToolSource, ToolDefinition, ToolResult, ToolExecutionContext
from .handler import ToolHandler, BuiltinToolHandler
from .registry import ToolRegistry
from .executor import ToolExecutor
from .approval import ApprovalManager
from .provider import ToolProvider

__all__ = [
    "ToolSource",
    "ToolDefinition",
    "ToolResult",
    "ToolExecutionContext",
    "ToolHandler",
    "BuiltinToolHandler",
    "ToolRegistry",
    "ToolExecutor",
    "ApprovalManager",
    "ToolProvider",
]
