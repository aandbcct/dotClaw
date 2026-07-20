"""Runtime 长期持久化的领域事实与 JSON 类型工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping, TypeAlias

from .control import AgentAction

if TYPE_CHECKING:
    from .context import StagedHistoryCompression, SuccessCommitIntent


JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
JSONMap: TypeAlias = dict[str, JSONValue]


class MessageRole(StrEnum):
    """运行消息的角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class RunMessageKind(StrEnum):
    """运行消息在执行证据中的用途。"""

    USER_INPUT = "user_input"
    # 仅用于读取阶段 A 之前的历史 messages.json；新写入路径改由 LLM_STARTED 事件审计调用。
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_RESULT = "tool_result"
    DELEGATION_RESULT = "delegation_result"
    FINAL_RESPONSE = "final_response"
    ERROR = "error"


class ContextCompactionScope(StrEnum):
    """版本化上下文摘要的持久化归属范围。"""

    SESSION_HISTORY = "session_history"


class SystemContextSlotStatus(StrEnum):
    """单个 system Slot 在冻结上下文中的产出状态。"""

    INCLUDED = "included"
    EMPTY = "empty"
    FAILED = "failed"


class SystemContextSlotScope(StrEnum):
    """冻结 system Slot 的缓存与变化边界。"""

    STATIC = "static"
    SESSION = "session"
    CONDITIONAL = "conditional"
    DYNAMIC = "dynamic"


class RunStatus(StrEnum):
    """一次运行的生命周期状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"


class RunErrorCode(StrEnum):
    """运行失败的标准化错误类别。"""

    LLM_FAILURE = "llm_failure"
    TOOL_FAILURE = "tool_failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_STATE = "invalid_state"
    PERSISTENCE_FAILURE = "persistence_failure"


class ApprovalStatus(StrEnum):
    """审批记录的消费状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"


@dataclass(frozen=True)
class ToolCall:
    """模型请求执行的一次工具调用。"""

    call_id: str
    name: str
    arguments: JSONMap

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {"call_id": self.call_id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class RunMessage:
    """一次运行中真实发送或接收的完整消息。"""

    message_id: str
    sequence: int
    kind: RunMessageKind
    role: MessageRole
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    metadata: JSONMap = field(default_factory=dict)

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "id": self.message_id,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "role": self.role.value,
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "tool_calls": [tool_call.to_dict() for tool_call in self.tool_calls],
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SystemContextSlot:
    """一次 Run 冻结的单个 system Slot 产物。"""

    name: str
    scope: SystemContextSlotScope
    status: SystemContextSlotStatus
    content: str = ""
    content_hash: str = ""
    error_code: str = ""

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 可持久化的 Slot 记录。"""
        return {
            "name": self.name,
            "scope": self.scope.value,
            "status": self.status.value,
            "content": self.content,
            "content_hash": self.content_hash,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class SystemContextSnapshot:
    """Run 开始时冻结的结构化 system context。"""

    version: int
    slot_order: tuple[str, ...]
    slots: tuple[SystemContextSlot, ...]
    rendered_content_hash: str

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的 system_context 区域。"""
        return {
            "version": self.version,
            "slot_order": list(self.slot_order),
            "slots": [slot.to_dict() for slot in self.slots],
            "rendered_content_hash": self.rendered_content_hash,
        }


@dataclass(frozen=True)
class HistoryMessageSnapshot:
    """冻结历史中来自指定 Conversation 的一条消息。"""

    conversation_id: str
    role: MessageRole
    content: str
    created_at: str = ""

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的近期历史记录。"""
        return {
            "conversation_id": self.conversation_id,
            "role": self.role.value,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class HistoryCompressionSnapshot:
    """某个 Session 历史压缩版本在 Run 中的冻结引用。"""

    compression_version: int
    covered_through_conversation_id: str
    content: str
    content_hash: str

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的压缩历史记录。"""
        return {
            "compression_version": self.compression_version,
            "covered_through_conversation_id": self.covered_through_conversation_id,
            "content": self.content,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class HistoryContextSnapshot:
    """Run 开始时实际注入的 Session 历史视图。"""

    source_session_id: str
    source_conversation_version: int
    recent_messages: tuple[HistoryMessageSnapshot, ...]
    content_hash: str
    compressed_history: HistoryCompressionSnapshot | None = None
    truncation_applied: bool = False

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的 history 区域。"""
        return {
            "source_session_id": self.source_session_id,
            "source_conversation_version": self.source_conversation_version,
            "compressed_history": None if self.compressed_history is None else self.compressed_history.to_dict(),
            "recent_messages": [message.to_dict() for message in self.recent_messages],
            "truncation_applied": self.truncation_applied,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class AgentPolicySnapshot:
    """运行期间不可变的 Agent 身份与执行策略。"""

    agent_id: str
    identity_version: str
    model_id: str
    max_iterations: int
    policy_data: JSONMap = field(default_factory=dict)

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "agent_id": self.agent_id,
            "identity_version": self.identity_version,
            "model_id": self.model_id,
            "max_iterations": self.max_iterations,
            "policy_data": self.policy_data,
        }


@dataclass(frozen=True)
class RunError:
    """运行失败时向调用方和持久化层提供的错误摘要。"""

    code: RunErrorCode
    message: str
    retryable: bool = False

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True)
class RunStatistics:
    """AgentRun 的最终聚合统计。"""

    duration_ms: int = 0
    llm_call_count: int = 0
    tool_call_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "duration_ms": self.duration_ms,
            "llm_call_count": self.llm_call_count,
            "tool_call_count": self.tool_call_count,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


@dataclass(frozen=True)
class AgentRun:
    """只保存索引和终态摘要的 AgentRun 持久化实体。"""

    run_id: str
    session_id: str
    agent_id: str
    status: RunStatus
    started_at: str
    policy: AgentPolicySnapshot
    input_message_id: str
    parent_run_id: str | None = None
    root_run_id: str | None = None
    ended_at: str | None = None
    resume_count: int = 0
    final_message_id: str | None = None
    latest_checkpoint_id: str | None = None
    active_context_version: int | None = None
    staged_history_compressions: tuple[StagedHistoryCompression, ...] = ()
    success_commit_intent: SuccessCommitIntent | None = None
    statistics: RunStatistics = field(default_factory=RunStatistics)
    error: RunError | None = None

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "resume_count": self.resume_count,
            "input_message_id": self.input_message_id,
            "final_message_id": self.final_message_id,
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "active_context_version": self.active_context_version,
            "staged_history_compressions": [candidate.to_dict() for candidate in self.staged_history_compressions],
            "success_commit_intent": (
                None if self.success_commit_intent is None else self.success_commit_intent.to_dict()
            ),
            "policy": self.policy.to_dict(),
            "statistics": self.statistics.to_dict(),
            "error": None if self.error is None else self.error.to_dict(),
        }


@dataclass(frozen=True)
class RunCheckpoint:
    """从安全边界恢复运行所需的最小快照。"""

    checkpoint_id: str
    run_id: str
    session_id: str
    checkpoint_sequence: int
    event_sequence: int
    message_sequence: int
    agent_state: JSONMap
    next_action: AgentAction
    pending: JSONMap
    budget: JSONMap
    active_context_version: int | None = None
    staged_history_compression_ids: tuple[str, ...] = ()

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "checkpoint_sequence": self.checkpoint_sequence,
            "event_sequence": self.event_sequence,
            "message_sequence": self.message_sequence,
            "agent_state": self.agent_state,
            "next_action": self.next_action.value,
            "pending": self.pending,
            "budget": self.budget,
            "active_context_version": self.active_context_version,
            "staged_history_compression_ids": list(self.staged_history_compression_ids),
        }


@dataclass(frozen=True)
class ApprovalRecord:
    """审批请求与所属运行的持久化关联。"""

    approval_id: str
    run_id: str
    session_id: str
    status: ApprovalStatus
    created_at: str
    metadata: JSONMap = field(default_factory=dict)

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


def utc_now_iso() -> str:
    """生成统一使用 UTC 的 ISO 8601 时间戳。"""
    current_time: datetime = datetime.now(UTC)
    return current_time.isoformat()


def require_json_map(value: JSONValue) -> JSONMap:
    """校验 JSON 值为对象并返回其精确类型。"""
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象")
    return value


def get_string(data: Mapping[str, JSONValue], field_name: str, default: str = "") -> str:
    """从 JSON 对象读取字符串字段。"""
    value: JSONValue | None = data.get(field_name)
    return value if isinstance(value, str) else default


def get_integer(data: Mapping[str, JSONValue], field_name: str, default: int = 0) -> int:
    """从 JSON 对象读取整数字段，避免布尔值误判为整数。"""
    value: JSONValue | None = data.get(field_name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default
