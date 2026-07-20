"""Context Slot 描述符与工厂注册表。"""

from __future__ import annotations

from collections.abc import Callable

from .contracts import ContextSlot, ContextSlotDescriptor


class ContextSlotRegistry:
    """按 Slot 标识保存 Descriptor 与构造器，不承担内容加载。"""
    def __init__(self) -> None:
        self._entries: dict[str, tuple[ContextSlotDescriptor, Callable[[], ContextSlot]]] = {}
    def register(self, descriptor: ContextSlotDescriptor, factory: Callable[[], ContextSlot]) -> None:
        """注册唯一 Slot 类型。"""
        if descriptor.slot_id in self._entries:
            raise ValueError(f"Context Slot 已注册：{descriptor.slot_id}")
        self._entries[descriptor.slot_id] = (descriptor, factory)
    def descriptor(self, slot_id: str) -> ContextSlotDescriptor:
        """查询已注册描述符。"""
        if slot_id not in self._entries:
            raise KeyError(f"未注册 Context Slot：{slot_id}")
        return self._entries[slot_id][0]
    def create(self, slot_id: str) -> ContextSlot:
        """创建 Slot 实例。"""
        return self._entries[slot_id][1]()
