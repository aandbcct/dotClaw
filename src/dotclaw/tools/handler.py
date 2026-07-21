"""工具执行器抽象接口（Tool v1 阶段一重构）。

Tool v1 阶段二起，内置工具统一使用 FunctionToolHandler（由 @tool + Discovery 构造），
BuiltinToolHandler 已删除。本模块只保留统一抽象接口。所有新增注释使用中文。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .base import ToolDefinition, ToolExecutionContext, ToolResult


class ToolHandler(ABC):
    """工具执行器的统一抽象接口"""

    @abstractmethod
    def definition(self) -> ToolDefinition:
        """返回工具定义"""
        ...

    @abstractmethod
    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行工具，返回结构化结果"""
        ...

    @property
    def name(self) -> str:
        return self.definition().name
