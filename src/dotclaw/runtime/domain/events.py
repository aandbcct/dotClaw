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
    RUN_ABANDONED = "run_abandoned"
    # 状态机拒绝非法迁移时记录的审计事实：只保存当前 mode/detail、事件类型与安全原因，
    # 不记录消息正文、完整工具参数等敏感负载。
    STATE_TRANSITION_REJECTED = "state_transition_rejected"


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


class ToolAuditStatus(StrEnum):
    """单个工具调用审计事件的终态，独立于 ToolPort 返回类别。"""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"
    CANCELLED = "cancelled"


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
    # 子运行已固化的 DELEGATION_RESULT 消息 ID，供父运行回灌；新状态机仅用
    # child_run_id 校验挂起控制标识，其余字段供下游恢复使用。
    result_message_id: str | None = None


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


# ============================================================================
# 新状态机（目标契约）判别联合事件
# ----------------------------------------------------------------------------
# 以下事件仅经 transition() 消费，表达「发生在 Engine 或外部控制边界的瞬时决策
# 事实」，不携带消息正文等审计负载；与上面的审计 RunEvent / RunEventType 严格区分。
# 旧 DomainEvent 与旧事件类仍被未迁移的应用层使用，调用方清零后删除。
# ============================================================================


@dataclass(frozen=True)
class LLMResponseProduced:
    """模型调用已完成并产出响应；``final`` 区分终态回答与需要继续调用工具。"""

    final: bool
    response_message_id: str | None = None
    tool_call_count: int = 0
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class LLMCallFailed:
    """模型调用不可恢复地失败；``error`` 仅用于下游诊断，不进入状态机。"""

    error: RunError | None = None
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ToolBatchCompleted:
    """工具批次已执行完成，可以回到模型调用。"""

    result_message_ids: tuple[str, ...] = ()
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ToolApprovalRequired:
    """工具批次需要人工审批；``approval_id`` 用于唤醒时校验归属。"""

    approval_id: str
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ToolBatchFailed:
    """工具批次执行失败；``error`` 仅用于下游诊断，不进入状态机。"""

    error: RunError | None = None
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ApprovalGranted:
    """审批交互层批准了等待中的审批；``approval_id`` 必须匹配挂起控制标识。"""

    approval_id: str
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ApprovalRejected:
    """审批交互层拒绝了等待中的审批；``approval_id`` 必须匹配挂起控制标识。"""

    approval_id: str
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class DelegationRequested:
    """模型请求把当前工具调用派发给子运行；由 Engine 执行 HANDOFF_TARGET。"""

    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class AbandonRequested:
    """显式放弃一个未结束的运行；由状态机收口为 Ended(ABANDONED)。"""

    reason: str = ""
    occurred_at: str = field(default_factory=utc_now_iso)


# 新状态机输入事件的判别联合类型。复用 RunStarted / DelegationSubmitted /
# DelegationCompleted / CancelRequested / TimeoutReached 这些字段已满足迁移需要
# 的既有事件类，避免重复定义；其余为新增分支。
AgentRunEvent = (
    RunStarted
    | LLMResponseProduced
    | LLMCallFailed
    | ToolBatchCompleted
    | ToolApprovalRequired
    | ToolBatchFailed
    | ApprovalGranted
    | ApprovalRejected
    | DelegationRequested
    | DelegationSubmitted
    | DelegationCompleted
    | CancelRequested
    | TimeoutReached
    | AbandonRequested
)
