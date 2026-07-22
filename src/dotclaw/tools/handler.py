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

    @property
    def args_model(self):
        """本地工具的 Pydantic 入参模型（总体设计 §4.1）。

        MCP Adapter 等无 Pydantic 模型的 Handler 返回 None，交由执行器按
        input_schema 走 JSON Schema 校验分支。
        """
        return None

    @property
    def input_schema(self) -> dict | None:
        """MCP 等外部工具的原始参数 JSON Schema（总体设计 §4.5）。

        本地工具返回 None，校验交由 args_model（Pydantic）完成。
        """
        return None
