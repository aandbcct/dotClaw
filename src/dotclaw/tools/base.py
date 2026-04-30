"""工具注册表"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .approval import ApprovalManager


logger = logging.getLogger("dotclaw.tools")


@dataclass
class ToolDefinition:
    """工具定义（描述 + 参数 schema）"""
    name: str
    description: str
    parameters: dict = field(default_factory=dict)  # JSON Schema


@dataclass
class ToolResult:
    """工具执行结果"""
    output: str
    is_error: bool = False


# 全局注册表
_registry: dict[str, tuple[ToolDefinition, Callable[..., Awaitable[Any]]]] = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict | None = None,
) -> Callable:
    """
    装饰器：注册一个异步工具函数。

    示例
    ----
    @register_tool(
        name="get_weather",
        description="查询城市天气",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"}
            },
            "required": ["city"]
        }
    )
    async def get_weather(city: str) -> str:
        return f"{city} 今天晴天，25°C"
    """
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
        _registry[name] = (
            ToolDefinition(
                name=name,
                description=description,
                parameters=parameters or {},
            ),
            func,
        )
        return func
    return decorator


class ToolRegistry:
    """工具注册表（生命周期管理）"""

    def __init__(self, approval_manager: ApprovalManager | None = None):
        self._tools: dict[str, tuple[ToolDefinition, Callable]] = dict(_registry)
        self._approval = approval_manager or ApprovalManager()

    def register(self, name: str, definition: ToolDefinition, handler: Callable):
        self._tools[name] = (definition, handler)

    def get_definitions(self) -> list[ToolDefinition]:
        """返回所有工具定义，用于传给 LLM"""
        return [def_ for def_, _ in self._tools.values()]

    def get_handler(self, name: str) -> Callable | None:
        handler = self._tools.get(name)
        return handler[1] if handler else None

    async def execute(
        self,
        name: str,
        arguments: dict,
        channel=None,
    ) -> ToolResult:
        """执行工具（含审批检查）"""
        entry = self._tools.get(name)
        if not entry:
            return ToolResult(output=f"错误：未找到工具 '{name}'", is_error=True)

        definition, handler = entry

        # 审批检查
        needs_approval = getattr(definition, "needs_approval", False)
        if needs_approval:
            approved = await self._approval.check(name, arguments, channel)
            if not approved:
                return ToolResult(output=f"用户拒绝了 {name} 的执行", is_error=True)

        try:
            result = await handler(**arguments)
            return ToolResult(output=str(result))
        except Exception as e:
            logger.exception(f"工具 {name} 执行出错")
            return ToolResult(output=f"工具执行出错: {e}", is_error=True)
