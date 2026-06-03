"""工具注册表（Phase 5 新增 — 从 base.py 拆出）"""

from __future__ import annotations
from typing import Any

from .base import ToolSource
from .handler import ToolHandler


class ToolRegistry:
    """纯工具注册表 — 只注册和查询，不执行"""

    def __init__(self):
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        """注册工具。同名后注册覆盖前注册（静默覆盖）。"""
        self._handlers[handler.name] = handler

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否成功删除。"""
        if name in self._handlers:
            del self._handlers[name]
            return True
        return False

    def get(self, name: str) -> ToolHandler | None:
        """按名称获取工具 Handler。"""
        return self._handlers.get(name)

    def get_definitions(self) -> list:
        """返回所有工具定义，用于传给 LLM 生成 tool_calls。"""
        return [h.definition() for h in self._handlers.values()]

    def list_by_source(self, source: ToolSource) -> list:
        """按来源列举工具。"""
        return [h for h in self._handlers.values() if h.definition().source == source]

    def all_names(self) -> list[str]:
        return list(self._handlers.keys())

    def clear(self) -> None:
        self._handlers.clear()
