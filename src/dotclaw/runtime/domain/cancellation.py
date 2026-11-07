"""取消控制的不可变数据；不承载运行结果或异步资源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .events import RunEvent, RunEventType
from .facts import AgentRun, JSONMap
from .state import Ended, RunOutcome


class CancellationOrigin(StrEnum):
    """取消请求的来源。"""

    USER = "user"
    PARENT = "parent"
    HOST_SHUTDOWN = "host_shutdown"


class CancellationPhase(StrEnum):
    """资源收尾进度，不是运行领域状态。"""

    REQUESTED = "requested"
    DRAINING = "draining"
    BLOCKED = "blocked"
    SETTLED = "settled"


class CancellationEffect(StrEnum):
    """外部副作用的确认范围。"""

    LOCAL_ONLY = "local_only"
    REMOTE_UNKNOWN = "remote_unknown"


class CancellationDisposition(StrEnum):
    """取消请求的受理结果。"""

    ACCEPTED = "accepted"
    ALREADY_REQUESTED = "already_requested"
    ALREADY_ENDED = "already_ended"
    COMMITTING_SUCCESS = "committing_success"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CancellationRequest:
    """首次取消请求，后续进度更新不能覆盖它。"""

    request_id: str
    run_id: str
    reason: str
    origin: CancellationOrigin
    requested_at: str
    parent_request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not self.run_id:
            raise ValueError("取消请求必须包含请求和运行标识")
        if not self.reason.strip() or len(self.reason) > 500:
            raise ValueError("取消原因必须非空且不超过 500 字符")
        timestamp = datetime.fromisoformat(self.requested_at.replace("Z", "+00:00"))
        if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("取消请求时间必须是 UTC ISO 时间")

    def to_dict(self) -> JSONMap:
        """转换为独立 JSON 载荷。"""
        return {"request_id": self.request_id, "run_id": self.run_id, "reason": self.reason,
                "origin": self.origin.value, "requested_at": self.requested_at,
                "parent_request_id": self.parent_request_id}


@dataclass(frozen=True)
class CancellationRecord:
    """带单调修订号的取消持久化记录。"""

    request: CancellationRequest
    revision: int
    phase: CancellationPhase
    pending_operation_ids: tuple[str, ...]
    pending_child_run_ids: tuple[str, ...]
    effect: CancellationEffect
    diagnostic_code: str | None
    updated_at: str
    settled_at: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1 or self.revision < 1:
            raise ValueError("取消记录必须为版本 1 且修订号为正数")
        for identifiers in (self.pending_operation_ids, self.pending_child_run_ids):
            if not isinstance(identifiers, tuple) or any(not isinstance(item, str) or not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
                raise ValueError("待清理资源必须是无重复的不可变标识集合")
        if (self.phase is CancellationPhase.SETTLED) != (self.settled_at is not None):
            raise ValueError("仅完成收尾的记录必须具有 settled_at")
        if self.phase is CancellationPhase.SETTLED and (self.pending_operation_ids or self.pending_child_run_ids):
            raise ValueError("完成收尾的记录不能包含待清理资源")

    def to_dict(self) -> JSONMap:
        """转换为取消记录文件载荷。"""
        return {"version": self.version, "request": self.request.to_dict(), "revision": self.revision,
                "phase": self.phase.value, "pending_operation_ids": list(self.pending_operation_ids),
                "pending_child_run_ids": list(self.pending_child_run_ids), "effect": self.effect.value,
                "diagnostic_code": self.diagnostic_code, "updated_at": self.updated_at, "settled_at": self.settled_at}


@dataclass(frozen=True)
class CancellationReceipt:
    """取消入口返回的受理回执。"""

    run_id: str
    disposition: CancellationDisposition
    request_id: str | None
    control_revision: int
    outcome: RunOutcome | None
    requested_at: str | None

    def to_dict(self) -> JSONMap:
        """转换为入口可投影的 JSON 数据。"""
        return {"run_id": self.run_id, "disposition": self.disposition.value, "request_id": self.request_id,
                "control_revision": self.control_revision, "outcome": self.outcome.value if self.outcome else None,
                "requested_at": self.requested_at}


@dataclass(frozen=True)
class CancellationCommitIntent:
    """固定取消决定和事件序号，支持中断后幂等补偿。"""

    request_id: str
    run: AgentRun
    events: tuple[RunEvent, ...]
    approval_ids: tuple[str, ...]
    settled_record: CancellationRecord
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1 or not isinstance(self.run.state.mode, Ended) or self.run.state.mode.outcome is not RunOutcome.CANCELLED:
            raise ValueError("取消提交必须是版本 1 的已取消运行")
        if self.settled_record.request.run_id != self.run.run_id or self.request_id != self.settled_record.request.request_id:
            raise ValueError("取消提交的请求和运行归属不一致")
        if self.settled_record.phase is not CancellationPhase.SETTLED:
            raise ValueError("取消提交必须携带 settled 记录")
        if not self.events or self.events[-1].event_type is not RunEventType.RUN_CANCELLED:
            raise ValueError("取消提交必须以 RUN_CANCELLED 结束")
        if sum(event.event_type is RunEventType.RUN_CANCELLED for event in self.events) != 1:
            raise ValueError("取消提交只能包含一个取消终态事件")
        if not isinstance(self.events, tuple) or not isinstance(self.approval_ids, tuple) or len(set(self.approval_ids)) != len(self.approval_ids):
            raise ValueError("取消提交事件和审批标识必须为不可变且无重复的集合")
        terminal_types = {RunEventType.RUN_COMPLETED, RunEventType.RUN_FAILED, RunEventType.RUN_ABANDONED, RunEventType.RUN_CANCELLED}
        if any(event.event_type in terminal_types for event in self.events[:-1]):
            raise ValueError("取消提交只能在末尾包含唯一终态事件")
        for index, event in enumerate(self.events):
            if event.run_id != self.run.run_id or event.sequence < 1:
                raise ValueError("取消事件归属或序号错误")
            if index and event.sequence != self.events[index - 1].sequence + 1:
                raise ValueError("取消提交事件序号必须连续")

    def to_dict(self) -> JSONMap:
        """转换为可恢复的取消提交文件。"""
        return {"version": self.version, "request_id": self.request_id, "run": self.run.to_dict(),
                "events": [event.to_dict() for event in self.events], "approval_ids": list(self.approval_ids),
                "settled_record": self.settled_record.to_dict()}
