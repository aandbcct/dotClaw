"""工具注册表（Tool v1 阶段一重构 — 从 base.py 拆出）。

职责边界：只负责无冲突注册、查询与不可变快照；不承担发现、验证、策略、
审批或执行。同名的静默覆盖已被移除（总体设计 §4.2 / §10.1 不变量 4）。
所有新增注释使用中文。
"""

from __future__ import annotations

import copy
from typing import Any

from .base import ToolDefinition, ToolSource
from .handler import ToolHandler


class DuplicateToolError(Exception):
    """同名工具重复注册，初始化必须失败（总体设计 §8.2）。

    携带冲突双方的名称与来源，便于定位冲突根因。
    """

    def __init__(self, name: str, existing_source: str, new_source: str) -> None:
        self.name = name
        self.existing_source = existing_source
        self.new_source = new_source
        super().__init__(
            f"工具名冲突: '{name}' 已由来源 '{existing_source}' 注册，"
            f"拒绝来源 '{new_source}' 的重复注册"
        )


class ToolRegistry:
    """纯工具注册表 — 只注册和查询，不执行。"""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        """注册工具。同名冲突必须使初始化失败，绝不静默覆盖。"""
        name = handler.name
        if name in self._handlers:
            existing = self._handlers[name].definition()
            incoming = handler.definition()
            raise DuplicateToolError(
                name,
                existing.source.value,
                incoming.source.value,
            )
        self._handlers[name] = handler

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否成功删除（供配置禁用处理）。"""
        if name in self._handlers:
            del self._handlers[name]
            return True
        return False

    def get(self, name: str) -> ToolHandler | None:
        """按名称获取工具 Handler。"""
        return self._handlers.get(name)

    def get_definitions(self) -> list[ToolDefinition]:
        """返回所有工具定义的快照（列表形式，供 LLM tool_calls 使用）。"""
        return list(self.snapshot())

    def snapshot(self) -> tuple[ToolDefinition, ...]:
        """返回当前可用工具定义的不可变快照。

        每个定义都是深拷贝；后续对 Registry 的增删或原 Handler 的修改都不会
        影响已取出的快照，满足 Run 级快照隔离需求（总体设计 §9）。
        """
        return tuple(copy.deepcopy(h.definition()) for h in self._handlers.values())

    def list_by_source(self, source: ToolSource) -> list[ToolHandler]:
        """按来源列举工具 Handler。"""
        return [
            h for h in self._handlers.values() if h.definition().source == source
        ]

    def all_names(self) -> list[str]:
        """返回所有已注册工具名（列表副本，防止外部修改内部字典）。"""
        return list(self._handlers.keys())

    def clear(self) -> None:
        """清空注册表（主要用于测试）。"""
        self._handlers.clear()
