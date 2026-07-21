"""Runtime v4 领域事件与审计事件模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .facts import JSONMap, RunError, utc_now_iso


class RunEventType(StrEnum):
    """需要持久化的运行事实类型。"""

    RUN_STARTED = "run_started"
    # 仅用于读取阶段 C 之前的历史 events.jsonl；新写入路径改由 LLM_STARTED 审计。
    CONTEXT_BUILT = "context_built"
    LLM_STARTED = "llm_started"
    LLM_COMPLETED = "llm_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    STATE_TRANSITION = "state_transition"
    CHECKPOINT_SAVED = "checkpoint_saved"
    WAITING_APPROVAL = "waiting_approval"
    APPROVAL_RESOLVED = "approval_resolved"
    RUN_RESUMED = "run_resumed"
    DELEGATION_SUBMITTED = "delegation_submitted"
    DELEGATION_COMPLETED = "delegation_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_ABANDONED = "run_abandoned"


class LLMCompletionKind(StrEnum):
    """一次模型调用完成后的控制结果。"""

    FINAL_RESPONSE = "final_response"
    TOOL_CALLS = "tool_calls"
    FAILED = "failed"


class ToolCompletionKind(StrEnum):
    """一次工具批次完成后的控制结果。"""

    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class RunEvent:
    """按运行序号追加的审计事实。"""

    run_id: str
    sequence: int
    event_type: RunEventType
    occurred_at: str
    message_ids: tuple[str, ...] = ()
    summary: str = ""
    data: JSONMap = field(default_factory=dict)

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at,
            "message_ids": list(self.message_ids),
            "summary": self.summary,
            "data": self.data,
        }


@dataclass(frozen=True)
class RunStarted:
    """新运行开始事件。"""

    input_message_id: str
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class LLMCompleted:
    """模型调用已完成的领域事件。"""

    kind: LLMCompletionKind
    response_message_id: str | None = None
    tool_call_count: int = 0
    error: RunError | None = None
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ToolCompleted:
    """工具调用批次已完成的领域事件。"""

    kind: ToolCompletionKind
    result_message_ids: tuple[str, ...] = ()
    approval_id: str | None = None
    error: RunError | None = None
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ApprovalResolved:
    """审批交互层提交的结构化审批结果。"""

    approval_id: str
    approved: bool
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class DelegationCompleted:
    """外部子执行完成事件。"""

    child_run_id: str
    succeeded: bool
    error: RunError | None = None
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class DelegationSubmitted:
    """父运行已提交一个结构化子运行请求。"""

    child_run_id: str
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class CancelRequested:
    """取消指定运行的控制事件。"""

    reason: str
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class TimeoutReached:
    """运行预算超时事件。"""

    timeout_ms: int
    occurred_at: str = field(default_factory=utc_now_iso)


DomainEvent = RunStarted | LLMCompleted | ToolCompleted | ApprovalResolved | DelegationSubmitted | DelegationCompleted | CancelRequested | TimeoutReached
