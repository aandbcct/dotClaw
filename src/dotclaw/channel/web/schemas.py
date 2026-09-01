"""GUI v0.1 HTTP 与 SSE 边界模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from ...runtime.domain.facts import JSONMap, MessageRole


class SSEEventType(StrEnum):
    """Web 消息流允许发送的事件类型。"""

    MESSAGE_DELTA = "message.delta"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_SUSPENDED = "run.suspended"
    STREAM_ERROR = "stream.error"


class WebRunStatus(StrEnum):
    """GUI 对外暴露的规范化运行状态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_DELEGATION = "waiting_delegation"
    CANCELLING = "cancelling"


class WebErrorCode(StrEnum):
    """HTTP/SSE 适配边界自身定义的错误码。"""

    INVALID_REQUEST = "invalid_request"
    UNKNOWN_IDENTITY = "unknown_identity"
    SESSION_CREATION_FAILED = "session_creation_failed"
    SESSION_NOT_FOUND = "session_not_found"
    NO_ACTIVE_RUN = "no_active_run"
    INTERNAL_ERROR = "internal_error"


class CreateSessionRequest(BaseModel):
    """创建 Session 的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")

    title: str = "新对话"
    agent_id: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """拒绝空标题并去除无意义的首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("title 不能为空")
        return normalized

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str | None) -> str | None:
        """把空白 Identity 视为未指定。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SubmitMessageRequest(BaseModel):
    """提交用户消息的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")

    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """拒绝空字符串和纯空白消息。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("content 不能为空")
        return normalized


class CancelSessionRequest(BaseModel):
    """按 Session 停止当前运行的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = "用户从 GUI 停止运行"

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """空取消原因回退为稳定的 GUI 默认原因。"""
        normalized = value.strip()
        return normalized or "用户从 GUI 停止运行"


class SessionSummaryResponse(BaseModel):
    """会话列表使用的稳定摘要。"""

    id: str
    title: str
    agent_id: str
    model: str
    created_at: str
    updated_at: str


class SessionMessageResponse(BaseModel):
    """从已完成 Conversation 展开的单条历史消息。"""

    id: str
    role: MessageRole
    content: str
    created_at: str


class SessionDetailResponse(SessionSummaryResponse):
    """会话详情与已完成历史消息。"""

    messages: list[SessionMessageResponse]


@dataclass(frozen=True)
class SSEEvent:
    """单个 SSE 传输事件；仅存在于内存，不作为 Runtime 事实落盘。"""

    event: SSEEventType
    data: JSONMap

    def __post_init__(self) -> None:
        """允许 HTTP 契约测试用字符串构造，同时立即收窄为枚举。"""
        if isinstance(self.event, str):
            object.__setattr__(self, "event", SSEEventType(self.event))
