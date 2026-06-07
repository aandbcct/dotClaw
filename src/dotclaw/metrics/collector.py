"""MetricsCollector — 零侵入事件采集器。

业务代码通过 on_event() 发布事件，采集器内部追加到事件流。
开启 is_active=False 可暂停采集，异常静默丢弃不影响主流程。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dotclaw.metrics.events import AgentEvent

logger = logging.getLogger("dotclaw.metrics.collector")


class MetricsCollector:
    """事件采集器：收集 Agent 运行时事件，纯内存操作。

    Attributes:
        is_active: True 时正常采集，False 时 on_event() 为 no-op。
    """

    def __init__(self) -> None:
        self._events: list["AgentEvent"] = []
        self.is_active: bool = True

    def on_event(self, event: "AgentEvent") -> None:
        """订阅 Agent 运行时事件。

        业务代码唯一调用入口。异常静默丢弃，不传播到调用方。
        """
        if not self.is_active:
            return
        try:
            self._events.append(event)
        except Exception:
            logger.debug("事件采集异常，已静默丢弃", exc_info=True)

    def clear(self) -> None:
        """清空已采集的全部事件。"""
        self._events.clear()

    @property
    def event_count(self) -> int:
        """已采集事件数量。"""
        return len(self._events)

    def get_events(self) -> list["AgentEvent"]:
        """返回事件列表的副本（只读快照）。"""
        return list(self._events)
