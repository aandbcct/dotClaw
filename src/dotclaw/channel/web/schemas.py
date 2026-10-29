"""GUI v0.1 HTTP 与 SSE 边界模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...runtime.domain.facts import JSONMap, MessageRole, RunError


class SSEEventType(StrEnum):
    """Web 消息流允许发送的事件类型。"""

    RUN_ACCEPTED = "run.accepted"
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
    RUNNING = "running"
    RECOVERABLE = "recoverable"
    ABANDONED = "abandoned"


class WebErrorCode(StrEnum):
    """HTTP/SSE 适配边界自身定义的错误码。"""

    INVALID_REQUEST = "invalid_request"
    UNKNOWN_IDENTITY = "unknown_identity"
    SESSION_CREATION_FAILED = "session_creation_failed"
    SESSION_NOT_FOUND = "session_not_found"
    NO_ACTIVE_RUN = "no_active_run"
    RUN_NOT_FOUND = "run_not_found"
    RUN_NOT_RECOVERABLE = "run_not_recoverable"
    SESSION_BUSY = "session_busy"
    INTERNAL_ERROR = "internal_error"


class CreateSessionRequest(BaseModel):
    """创建 Session 的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")

    title: str = "新对话"
    agent_id: str | None = None
    model: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """拒绝空标题并去除无意义的首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("title 不能为空")
        return normalized

    @field_validator("agent_id", "model")
    @classmethod
    def validate_agent_id(cls, value: str | None) -> str | None:
        """把空白 Identity 视为未指定。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ResolveApprovalRequest(BaseModel):
    """网页提交的审批决定。"""

    model_config = ConfigDict(extra="forbid")
    approved: bool


class SwitchModelRequest(BaseModel):
    """只允许修改后续新运行的模型绑定。"""

    model_config = ConfigDict(extra="forbid")
    model: str

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """拒绝空模型名称。"""
        if not value.strip():
            raise ValueError("模型不能为空")
        return value.strip()


class SearchHitResponse(BaseModel):
    """一条历史标题或正文命中，消息标识与详情页一致。"""

    session_id: str
    title: str
    updated_at: str
    archived: bool
    message_id: str | None = None
    role: MessageRole | None = None
    snippet: str


class SearchSessionsResponse(BaseModel):
    """有界的历史字面搜索结果。"""

    items: list[SearchHitResponse]
    has_more: bool


class ToggleSettingRequest(BaseModel):
    """切换单个运行时能力。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


class UpdateSessionRequest(BaseModel):
    """仅允许修改会话标题和归档状态。"""

    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> UpdateSessionRequest:
        """拒绝空更新、显式空值与空白标题。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个更新字段")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError("更新字段不能为 null")
        if self.title is not None:
            self.title = self.title.strip()
            if not self.title:
                raise ValueError("标题不能为空")
        return self


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
    archived: bool = False
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
    run_ids: list[str] = Field(default_factory=list)


class SessionDetailResponse(SessionSummaryResponse):
    """会话详情与已完成历史消息。"""

    messages: list[SessionMessageResponse]


class AgentOptionResponse(BaseModel):
    """新建会话可选的 Agent 摘要。"""

    id: str
    name: str
    description: str


class WorkspaceOptionsResponse(BaseModel):
    """GUI 新建会话所需的静态选项。"""

    agents: list[AgentOptionResponse]
    models: list[str]
    default_agent_id: str
    default_model: str


class ModelSettingResponse(BaseModel):
    """脱敏后的模型配置摘要。"""

    name: str
    provider: str
    model_id: str
    context_window: int
    capabilities: list[str]
    status: str
    reasoning_mode: str
    api_key_configured: bool
    preferred: bool = False


class GeneralSettingResponse(BaseModel):
    """当前进程正在使用的通用配置。"""

    stream: bool
    max_context_tokens: int
    keep_recent_messages: int
    exec_timeout: float
    scheduler_enabled: bool


class ToolSettingResponse(BaseModel):
    """工具目录中的单项状态。"""

    name: str
    description: str
    source: str
    enabled: bool
    needs_approval: bool


class SkillSettingResponse(BaseModel):
    """Skill 目录中的单项状态。"""

    name: str
    description: str
    emoji: str = ""
    enabled: bool
    always_load: bool


class McpServerSettingResponse(BaseModel):
    """脱敏后的 MCP server 配置与连接状态。"""

    name: str
    transport: str
    endpoint: str
    state: str
    detail: str = ""
    tool_count: int = 0


class RuntimeSettingsResponse(BaseModel):
    """网页设置中心使用的真实运行时快照。"""

    general: GeneralSettingResponse
    models: list[ModelSettingResponse]
    tools: list[ToolSettingResponse]
    skills: list[SkillSettingResponse]
    mcp_enabled: bool
    mcp_servers: list[McpServerSettingResponse]


class ToolExecutionResponse(BaseModel):
    """单次工具调用的可折叠展示数据。"""

    call_id: str
    name: str
    status: str
    started_at: str = ""
    completed_at: str = ""
    arguments: dict[str, Any]
    result: str = ""
    error: str = ""


class RunStepResponse(BaseModel):
    """按运行事件序号稳定关联的模型或工具步骤。"""

    id: str
    sequence: int
    kind: Literal["model", "tool"]
    status: Literal["running", "completed", "failed", "cancelled", "approval_required", "interrupted"]
    started_at: str = ""
    completed_at: str = ""
    call_id: str | None = None


class RunActivityResponse(BaseModel):
    """当前或指定运行的状态与工具执行过程。"""

    run_id: str
    status: str
    approval_id: str | None = None
    approval_call_id: str | None = None
    child_run_id: str | None = None
    tools: list[ToolExecutionResponse]
    error: RunError | None = None
    steps: list[RunStepResponse] = Field(default_factory=list)
    event_sequence: int = 0


@dataclass(frozen=True)
class SSEEvent:
    """单个 SSE 传输事件；仅存在于内存，不作为 Runtime 事实落盘。"""

    event: SSEEventType
    data: JSONMap

    def __post_init__(self) -> None:
        """允许 HTTP 契约测试用字符串构造，同时立即收窄为枚举。"""
        if isinstance(self.event, str):
            object.__setattr__(self, "event", SSEEventType(self.event))
