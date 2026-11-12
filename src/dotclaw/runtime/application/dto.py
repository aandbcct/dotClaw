"""Runtime 应用层在请求、上下文和外部 Port 间传递的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Literal

from ...common.tool_replay import ToolExecutionEvidence, ToolReplayPolicy, ToolSideEffect

from ..domain.facts import (
    HistoryCompressionSnapshot,
    AgentRun,
    JSONMap,
    MessageRole,
    RunCheckpoint,
    RunError,
    RunMessage,
    ToolCall,
)
from ..domain.state import AgentRunState, Ended, RunOutcome
from ..domain.context import ContextOwner, ContextRefreshReason, ContextSlotSnapshot
from ..domain.events import RunEvent
from ..domain.tool_recovery import ToolCallLedger, ToolOutcomeSnapshot, ToolRecoveryAction


@dataclass(frozen=True)
class ConversationMessage:
    """应用层冻结的单条会话消息。"""

    message_id: str
    role: MessageRole
    content: str
    created_at: str

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "id": self.message_id,
            "role": self.role.value,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ConversationSnapshot:
    """创建 Run 时冻结的会话视图，不是 Session 的持久化实体。"""

    session_id: str
    messages: tuple[ConversationMessage, ...]
    version: int
    compressed_history: HistoryCompressionSnapshot | None = None

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "session_id": self.session_id,
            "messages": [message.to_dict() for message in self.messages],
            "version": self.version,
            "compressed_history": (
                None if self.compressed_history is None else self.compressed_history.to_dict()
            ),
        }


@dataclass(frozen=True)
class RunRequest:
    """Application 提交给 RuntimeEngine 的单次运行请求。"""

    session_id: str
    lease_id: str
    agent_id: str
    user_message: ConversationMessage
    conversation: ConversationSnapshot
    parent_run_id: str | None = None
    root_run_id: str | None = None
    run_id: str = ""
    model_id: str = ""
    delegation_task_id: str | None = None

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典，供执行期诊断使用。"""
        return {
            "session_id": self.session_id,
            "lease_id": self.lease_id,
            "agent_id": self.agent_id,
            "user_message": self.user_message.to_dict(),
            "conversation": self.conversation.to_dict(),
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "run_id": self.run_id,
            "model_id": self.model_id,
            "delegation_task_id": self.delegation_task_id,
        }


@dataclass(frozen=True)
class RunResult:
    """Application 返回给入口层的单次执行结果。"""

    run_id: str
    state: AgentRunState
    final_message: ConversationMessage | None = None
    error: RunError | None = None
    approval_id: str | None = None
    child_run_id: str | None = None
    """delegation 挂起时携带的子运行标识，供入口层触发恢复。"""
    has_streamed_response: bool = False
    """本次运行是否已通过 LLMOutputPort 向入口输出过面向用户的 response 文本。"""
    control_status: Literal["cancelling", "recoverable"] | None = None
    """仅描述非终态的取消收尾或可恢复控制状态。"""

    def to_dict(self) -> JSONMap:
        """转换为 Channel 或 API 可消费的结果数据。"""
        return {
            "run_id": self.run_id,
            "state": self.state.to_dict(),
            "final_message": None if self.final_message is None else self.final_message.to_dict(),
            "error": None if self.error is None else self.error.to_dict(),
            "approval_id": self.approval_id,
            "child_run_id": self.child_run_id,
            "has_streamed_response": self.has_streamed_response,
            "control_status": self.control_status,
        }


@dataclass(frozen=True)
class RunAcceptedEvent:
    """新运行持久化后的接收确认，不代表供应商已接收请求。"""

    session_id: str
    run_id: str


class LLMOutputKind(StrEnum):
    """LLM 增量输出的语义类别，由 Runtime 解释并交付入口层。"""

    REASONING_DELTA = "reasoning_delta"
    RESPONSE_DELTA = "response_delta"


@dataclass(frozen=True)
class LLMOutputEvent:
    """一次 LLM 调用产生的有序增量输出事件。

    不增加 sequence / call_index：单次调用交付顺序由串行异步迭代器保证，
    Run 全局顺序继续由既有 RunEvent.sequence 表达（总体设计 §3.3）。
    """

    session_id: str
    run_id: str
    kind: LLMOutputKind
    content: str


@dataclass(frozen=True)
class ToolDefinition:
    """Application 提供给 LLMPort 的可调用工具定义。"""

    name: str
    description: str
    parameters: JSONMap
    replay_policy: ToolReplayPolicy = ToolReplayPolicy.NO_AUTO_RETRY
    replay_contract_version: str = ""
    side_effect: ToolSideEffect = ToolSideEffect.UNKNOWN
    recovery_max_retries: int = 0
    recovery_initial_delay_s: float = 1.0
    recovery_backoff_multiplier: float = 2.0
    recovery_max_delay_s: float = 30.0

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass(frozen=True)
class ContextMetadata:
    """Application 上下文构建过程的裁剪与来源信息。"""

    estimated_tokens: int
    source_names: tuple[str, ...] = ()
    truncation_applied: bool = False
    details: JSONMap = field(default_factory=dict)
    slot_snapshots: tuple[ContextSlotSnapshot, ...] = ()
    fact_reference_message_ids: tuple[str, ...] = ()

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "estimated_tokens": self.estimated_tokens,
            "source_names": list(self.source_names),
            "truncation_applied": self.truncation_applied,
            "details": self.details,
            "slot_snapshots": [snapshot.to_dict() for snapshot in self.slot_snapshots],
            "fact_reference_message_ids": list(self.fact_reference_message_ids),
        }


@dataclass(frozen=True)
class ContextBundle:
    """一次 LLM 调用实际使用的上下文与工具定义。"""

    messages: tuple[RunMessage, ...]
    tools: tuple[ToolDefinition, ...]
    metadata: ContextMetadata

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "messages": [message.to_dict() for message in self.messages],
            "tools": [tool.to_dict() for tool in self.tools],
            "metadata": self.metadata.to_dict(),
        }


@dataclass(frozen=True)
class ContextRefreshSignal:
    """通过 ContextPort 发布的定向 Slot 刷新事件 DTO。"""

    slot_id: str
    owner: ContextOwner
    owner_key: str
    reason: ContextRefreshReason
    payload: JSONMap


class ToolResultStatus(StrEnum):
    """ToolPort 返回给 Application 的工具执行结果类别。"""

    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"


class ToolStepKind(StrEnum):
    """账本工具步骤在当前驱动中的控制结果。"""

    COMPLETED = "completed"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"
    CANCEL_PENDING = "cancel_pending"


@dataclass(frozen=True)
class ToolStepResult:
    """单工具步骤的完整结果，避免用 None 混淆审批、阻断与取消。"""

    kind: ToolStepKind
    outcome: ToolOutcomeSnapshot | None = None
    run_result: "RunResult | None" = None

    def __post_init__(self) -> None:
        if self.kind is ToolStepKind.COMPLETED:
            if self.outcome is None or self.run_result is not None:
                raise ValueError("completed 工具步骤必须且仅能包含 outcome")
        elif self.outcome is not None or self.run_result is None:
            raise ValueError("非 completed 工具步骤必须且仅能包含 run_result")


@dataclass(frozen=True)
class ToolInvocation:
    """Application 提交给 ToolPort 的一次工具调用。"""

    run_id: str
    call: ToolCall
    approved: bool = False
    """恢复场景下该调用已获批准，适配器应直接执行而非再次请求审批。"""
    attempt: int = 1
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """ToolPort 返回给 Application 的标准化工具执行结果。"""

    call_id: str
    status: ToolResultStatus
    output: str = ""
    approval_id: str | None = None
    error: RunError | None = None
    execution_evidence: ToolExecutionEvidence = ToolExecutionEvidence.UNKNOWN
    tool_error_code: str | None = None
    tool_error_type: str | None = None


@dataclass(frozen=True)
class ToolActivityScan:
    """按历史事件解释出的工具活动，不作为持久账本。"""

    active: dict[str, RunEvent]
    waiting: dict[str, RunEvent]
    terminal_call_ids: frozenset[str]


@dataclass(frozen=True)
class DelegationRequest:
    """Application 提交给 DelegationPort 的子执行请求。"""

    parent_run_id: str
    root_run_id: str
    target_agent_id: str
    input_message: ConversationMessage
    source_agent_id: str = ""
    source_session_id: str = ""
    source_tool_call_id: str = ""


@dataclass(frozen=True)
class DelegationSubmission:
    """DelegationPort 已受理的子运行和 Task 队列关联信息。"""

    child_run_id: str
    task_id: str
    target_session_id: str


@dataclass(frozen=True)
class DelegationResult:
    """DelegationPort 返回给 Application 的子执行结果。

    不再依赖旧状态枚举：以 ``outcome`` 暴露子运行是否已终态及结果类别，
    未结束或不可判定时为 ``None``；其余字段供父运行回灌 delegation result 消息。
    """

    child_run_id: str
    outcome: RunOutcome | None
    output: str = ""
    error: RunError | None = None


@dataclass(frozen=True)
class ToolRecoveryContract:
    """工具端口只读返回的恢复安全契约。"""

    policy: ToolReplayPolicy = ToolReplayPolicy.NO_AUTO_RETRY
    version: str = ""
    key_valid_until: str | None = None
    side_effect: ToolSideEffect = ToolSideEffect.UNKNOWN
    recovery_max_retries: int = 0
    recovery_initial_delay_s: float = 1.0
    recovery_backoff_multiplier: float = 2.0
    recovery_max_delay_s: float = 30.0

    def __post_init__(self) -> None:
        if self.policy in {ToolReplayPolicy.SAFE_RETRY, ToolReplayPolicy.KEYED_IDEMPOTENT} and not self.version:
            raise ValueError("安全恢复契约必须声明非空版本")
        if self.policy is ToolReplayPolicy.SAFE_RETRY and self.side_effect is not ToolSideEffect.NONE:
            raise ValueError("SAFE_RETRY 仅允许副作用声明为 NONE 的工具")
        if not isinstance(self.recovery_max_retries, int) or isinstance(self.recovery_max_retries, bool) or self.recovery_max_retries < 0:
            raise ValueError("recovery_max_retries 必须是非负整数")
        values = (self.recovery_initial_delay_s, self.recovery_backoff_multiplier, self.recovery_max_delay_s)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) for value in values):
            raise ValueError("恢复退避参数必须是数字")
        if self.recovery_initial_delay_s < 0 or self.recovery_backoff_multiplier < 1 or self.recovery_max_delay_s < self.recovery_initial_delay_s:
            raise ValueError("恢复退避参数范围无效")
        if self.key_valid_until is not None:
            try:
                timestamp = datetime.fromisoformat(self.key_valid_until[:-1] + "+00:00" if self.key_valid_until.endswith("Z") else self.key_valid_until)
            except ValueError as error:
                raise ValueError("key_valid_until 必须是 UTC ISO 时间或 null") from error
            if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
                raise ValueError("key_valid_until 必须是 UTC ISO 时间或 null")


@dataclass(frozen=True)
class ToolRecoveryDecision:
    """恢复规划器对一个工具调用的决定。"""

    call_id: str
    action: ToolRecoveryAction
    reason: str

    def __post_init__(self) -> None:
        if not self.call_id or not isinstance(self.reason, str):
            raise ValueError("恢复决定必须关联 call_id 并提供原因")


@dataclass(frozen=True)
class ToolRecoveryPlan:
    """重建后的工具批次、控制快照及下一可用消息/事件序号。"""

    run: AgentRun
    messages: tuple[RunMessage, ...]
    checkpoint: RunCheckpoint | None
    ledger: ToolCallLedger
    next_event_sequence: int
    next_message_sequence: int
    decisions: tuple[ToolRecoveryDecision, ...]

    def __post_init__(self) -> None:
        if self.run.run_id != self.ledger.run_id or self.run.session_id != self.ledger.session_id:
            raise ValueError("ToolRecoveryPlan 的运行身份与账本不一致")
        if not isinstance(self.next_event_sequence, int) or isinstance(self.next_event_sequence, bool) or self.next_event_sequence < 1:
            raise ValueError("next_event_sequence 必须是正整数")
        if not isinstance(self.next_message_sequence, int) or isinstance(self.next_message_sequence, bool) or self.next_message_sequence < 1:
            raise ValueError("next_message_sequence 必须是正整数")


def run_outcome_from_state(state: AgentRunState) -> RunOutcome | None:
    """将终态 ``AgentRunState`` 投影为 ``RunOutcome``；非终态返回 ``None``。"""
    return state.mode.outcome if isinstance(state.mode, Ended) else None
