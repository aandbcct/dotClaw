"""Context Slot 的进程内类型化刷新信号。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ContextRefreshSignal:
    """请求指定 Slot 在下一安全点刷新。"""
    slot_id: str
    reason: str


class ContextSignalSubscription(Protocol):
    """SlotManager 持有的定向信号订阅句柄。"""

    @property
    def slot_id(self) -> str:
        """返回订阅的唯一 Slot 标识。"""


@dataclass(frozen=True)
class _InMemorySubscription:
    """进程内总线的不可变订阅句柄。"""

    slot_id: str


class ContextSignalBus:
    """仅在进程内暂存刷新信号，不承诺可靠投递。"""
    def __init__(self) -> None:
        self._signals: list[ContextRefreshSignal] = []
        self._subscriber_slot_ids: set[str] = set()

    def subscribe(self, slot_id: str) -> ContextSignalSubscription:
        """登记可接收指定 Slot 定向刷新的 Manager 订阅。"""
        self._subscriber_slot_ids.add(slot_id)
        return _InMemorySubscription(slot_id)
    def publish(self, signal: ContextRefreshSignal) -> None:
        """发布一条类型化刷新请求。"""
        self._signals.append(signal)
    def drain(self) -> tuple[ContextRefreshSignal, ...]:
        """取出当前所有信号。"""
        signals: tuple[ContextRefreshSignal, ...] = tuple(
            signal for signal in self._signals if signal.slot_id in self._subscriber_slot_ids
        )
        self._signals.clear()
        return signals
