"""工具执行器抽象接口（Phase 5 新增）"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from .base import ToolDefinition, ToolExecutionContext, ToolResult, ToolSource

logger = logging.getLogger("dotclaw.tools.handler")


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


class BuiltinToolHandler(ToolHandler):
    """内置工具适配器 — 将现有异步函数包装为 ToolHandler"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler_fn: Callable[..., Awaitable[Any]],
        needs_approval: bool = False,
        timeout: float = 60.0,
        metadata: dict | None = None,
    ):
        self._definition = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            source=ToolSource.BUILTIN,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata=metadata or {},
        )
        self._handler_fn = handler_fn

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        _ = context  # Phase 5: context 暂未在 builtin handler 中使用
        try:
            result = await self._handler_fn(**arguments)
            return ToolResult(output=str(result))
        except Exception as e:
            logger.exception(f"工具 {self._definition.name} 执行出错")
            return ToolResult(
                output=f"工具执行出错: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )
