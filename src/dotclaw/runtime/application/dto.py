"""Runtime 应用层在请求、上下文和外部 Port 间传递的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..domain.facts import (
    HistoryCompressionSnapshot,
    JSONMap,
    MessageRole,
    RunError,
    RunMessage,
    RunStatus,
    ToolCall,
)
from ..domain.context import ContextSlotSnapshot


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
        }


@dataclass(frozen=True)
class RunResult:
    """Application 返回给入口层的单次执行结果。"""

    run_id: str
    status: RunStatus
    final_message: ConversationMessage | None = None
    error: RunError | None = None
    approval_id: str | None = None
    has_streamed_text: bool = False
    """本次运行是否已通过 TextStreamPort 向入口输出过文本。"""

    def to_dict(self) -> JSONMap:
        """转换为 Channel 或 API 可消费的结果数据。"""
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "final_message": None if self.final_message is None else self.final_message.to_dict(),
            "error": None if self.error is None else self.error.to_dict(),
            "approval_id": self.approval_id,
            "has_streamed_text": self.has_streamed_text,
        }


@dataclass(frozen=True)
class ToolDefinition:
    """Application 提供给 LLMPort 的可调用工具定义。"""

    name: str
    description: str
    parameters: JSONMap

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

    def to_dict(self) -> JSONMap:
        """转换为 JSON 兼容字典。"""
        return {
            "estimated_tokens": self.estimated_tokens,
            "source_names": list(self.source_names),
            "truncation_applied": self.truncation_applied,
            "details": self.details,
            "slot_snapshots": [snapshot.to_dict() for snapshot in self.slot_snapshots],
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


class ToolResultStatus(StrEnum):
    """ToolPort 返回给 Application 的工具执行结果类别。"""

    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class ToolInvocation:
    """Application 提交给 ToolPort 的一次工具调用。"""

    run_id: str
    call: ToolCall


@dataclass(frozen=True)
class ToolResult:
    """ToolPort 返回给 Application 的标准化工具执行结果。"""

    call_id: str
    status: ToolResultStatus
    output: str = ""
    approval_id: str | None = None
    error: RunError | None = None


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
    """DelegationPort 返回给 Application 的子执行结果。"""

    child_run_id: str
    status: RunStatus
    output: str = ""
    error: RunError | None = None
