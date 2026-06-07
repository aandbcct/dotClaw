"""MetricsCollector — 零侵入事件采集器。

业务代码通过 on_event() 发布事件，采集器内部追加到事件流。
开启 is_active=False 可暂停采集，异常静默丢弃不影响主流程。
调用 finalize() 将事件流计算为快照并保存到文件。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dotclaw.metrics.events import AgentEvent
    from dotclaw.metrics.snapshot import RunMeta

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

    def finalize(self, run_meta: "RunMeta", output_dir: str = "data/snapshots", task_count: int = 1) -> str | None:
        """将已采集的事件流计算为快照并保存到文件。

        在会话结束时调用，自动生成快照 JSON。

        Args:
            run_meta: 运行元信息。
            output_dir: 快照输出目录。
            task_count: 任务总数，用于 per_task 指标计算。

        Returns:
            保存的文件路径；若无事件或构建失败则返回 None。
        """
        if not self._events:
            logger.debug("无事件可生成快照，跳过保存")
            return None

        try:
            from dotclaw.metrics.builder import SnapshotBuilder

            builder = SnapshotBuilder(run_meta, task_count=task_count)
            for event in self._events:
                builder.process(event)
            snapshot = builder.build()

            from dotclaw.metrics.storage import save_snapshot

            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            filepath = output_path / f"{run_meta.run_id}.json"
            save_snapshot(snapshot, str(filepath))
            logger.info(f"指标快照已保存: {filepath}")
            self._events.clear()  # 保存后清空，避免跨会话事件累积
            return str(filepath)
        except Exception:
            logger.warning("快照生成失败", exc_info=True)
            return None
