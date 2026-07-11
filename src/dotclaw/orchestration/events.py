"""DelegationEvent —— delegation 生命周期事件。

事件记录任务委托从提交到完成/失败/取消的不可变事实，用于 trace、审计、恢复和调试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from .task import JsonValue, TaskTargetKind


class DelegationEventType(Enum):
    """delegation 生命周期事件类型。"""

    SUBMITTED = "submitted"
    """任务已提交"""

    STARTED = "started"
    """任务已开始执行"""

    COMPLETED = "completed"
    """任务已完成"""

    FAILED = "failed"
    """任务执行失败"""

    CANCELLED = "cancelled"
    """任务已取消"""

    TIMEOUT = "timeout"
    """任务等待超时"""


@dataclass
class DelegationEvent:
    """delegation 生命周期事件。"""

    task_id: str
    """本地 Task ID"""

    handle_id: str
    """本地 Handle ID"""

    parent_agent_id: str
    """父 Agent ID"""

    target_agent_id: str
    """目标 Agent ID"""

    target_kind: TaskTargetKind
    """目标 Agent 类型"""

    event_type: DelegationEventType
    """事件类型"""

    payload: dict[str, JsonValue] = field(default_factory=dict)
    """事件载荷"""

    event_id: str = field(default_factory=lambda: uuid4().hex)
    """事件 ID"""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    """事件时间"""

    def to_dict(self) -> dict[str, JsonValue]:
        """序列化为 dict。"""
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "handle_id": self.handle_id,
            "parent_agent_id": self.parent_agent_id,
            "target_agent_id": self.target_agent_id,
            "target_kind": self.target_kind.value,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }
