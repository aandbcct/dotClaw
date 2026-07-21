"""Runtime v4 上下文版本与提交控制面的领域事实。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from .facts import JSONMap, JSONValue, MessageRole, RunStatus, get_integer, get_string, require_json_map, utc_now_iso


class ContextContributionKind(StrEnum):
    """上下文槽贡献的封闭载荷类型。"""

    SYSTEM_CONTENT = "system_content"
    TOOL_DEFINITIONS = "tool_definitions"
    HISTORY_COMPRESSIONS = "history_compressions"
    CONVERSATION_MESSAGES = "conversation_messages"
    RUN_MESSAGE_REFERENCES = "run_message_references"


class ContextPersistenceMode(StrEnum):
    """Slot 内容在 Context Version 中的持久化方式。"""

    SNAPSHOT = "snapshot"
    FACT_REFERENCE = "fact_reference"


class ContextSlotStatus(StrEnum):
    """已绑定上下文槽在某个版本中的加载结果。"""

    INCLUDED = "included"
    EMPTY = "empty"
    FAILED = "failed"


class ContextOwner(StrEnum):
    """上下文数据的唯一领域所有者。"""

    AGENT = "agent"
    SESSION = "session"
    RUN = "run"
    GLOBAL = "global"


class ContextRefreshReason(StrEnum):
    """触发 Context Slot 刷新的标准原因。"""

    OWNER_DATA_CHANGED = "owner_data_changed"
    CONFIGURATION_CHANGED = "configuration_changed"
    EXTERNAL_SOURCE_CHANGED = "external_source_changed"


class StagedHistoryCompressionStatus(StrEnum):
    """Run 内历史压缩候选的生命周期状态。"""

    STAGED = "staged"
    SUPERSEDED = "superseded"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class RunInterruptedReason(StrEnum):
    """可恢复中断的明确原因。"""

    LLM_UNAVAILABLE = "llm_unavailable"
    PROCESS_RESTART = "process_restart"


class RunAbandonedReason(StrEnum):
    """中断 Run 被放弃的明确原因。"""

    SUPERSEDED_BY_NEW_QUERY = "superseded_by_new_query"
    USER_REQUESTED = "user_requested"


class SuccessCommitFaultPoint(StrEnum):
    """成功提交恢复流程可注入的故障边界。"""

    BEFORE_SESSION_PROJECTION = "before_session_projection"
    AFTER_SESSION_PROJECTION = "after_session_projection"
    BEFORE_COMPLETED_EVENT = "before_completed_event"
    AFTER_COMPLETED_EVENT = "after_completed_event"
    BEFORE_RUN_FINALIZATION = "before_run_finalization"
    AFTER_RUN_FINALIZATION = "after_run_finalization"


@dataclass(frozen=True)
class TextSlotContent:
    """系统文本或历史摘要的直接 Slot 正文。"""

    text: str


@dataclass(frozen=True)
class ToolDefinitionSlotContent:
    """可审计的单个实际工具 Schema。"""

    name: str
    description: str
    parameters: JSONMap

    def to_dict(self) -> JSONMap:
        """转换为稳定的工具 Schema 记录。"""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass(frozen=True)
class ToolDefinitionsSlotContent:
    """经过 Agent 策略筛选后的工具定义集合。"""

    tools: tuple[ToolDefinitionSlotContent, ...]


@dataclass(frozen=True)
class ConversationSlotMessage:
    """Conversation Slot 中可直接审计的一条会话消息。"""

    message_id: str
    role: MessageRole
    content: str
    created_at: str

    def to_dict(self) -> JSONMap:
        """转换为稳定消息记录。"""
        return {"id": self.message_id, "role": self.role.value, "content": self.content, "created_at": self.created_at}


@dataclass(frozen=True)
class ConversationMessagesSlotContent:
    """压缩边界后的完整 Conversation 消息集合。"""

    messages: tuple[ConversationSlotMessage, ...]


@dataclass(frozen=True)
class RunMessageReferencesSlotContent:
    """事实引用型 Slot 使用的 Run Message 标识集合。"""

    message_ids: tuple[str, ...]


ContextSlotContent: TypeAlias = (
    TextSlotContent
    | ToolDefinitionsSlotContent
    | ConversationMessagesSlotContent
    | RunMessageReferencesSlotContent
)


@dataclass(frozen=True)
class ContextSlotSnapshot:
    """一个已绑定快照型 Slot 的不可变、可审计载荷。"""

    slot_id: str
    owner: ContextOwner
    contribution_kind: ContextContributionKind
    persistence_mode: ContextPersistenceMode
    status: ContextSlotStatus
    injection_order: int
    content: ContextSlotContent
    content_hash: str = ""
    error_code: str = ""

    def to_dict(self) -> JSONMap:
        """按贡献类型转换为 messages.json 的稳定 Slot 记录。"""
        return {
            "slot_id": self.slot_id,
            "owner": self.owner.value,
            "contribution_kind": self.contribution_kind.value,
            "persistence_mode": self.persistence_mode.value,
            "status": self.status.value,
            "injection_order": self.injection_order,
            "content": _content_to_json(self.content),
            "content_hash": self.content_hash,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class ContextVersion:
    """一次业务模型调用前形成的完整、不可变快照上下文版本。"""

    version: int
    created_at: str
    slots: tuple[ContextSlotSnapshot, ...]
    content_hash: str
    tool_schema_hash: str

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的上下文版本记录。"""
        return {"version": self.version, "created_at": self.created_at, "slots": [slot.to_dict() for slot in self.slots], "content_hash": self.content_hash, "tool_schema_hash": self.tool_schema_hash}


@dataclass(frozen=True)
class StagedHistoryCompression:
    """尚未提交到 Session 的历史压缩候选控制信息。"""

    candidate_id: str
    status: StagedHistoryCompressionStatus
    session_baseline_version: int
    covered_through_conversation_id: str
    source_hash: str
    summary_hash: str
    context_version: int

    def to_dict(self) -> JSONMap:
        """转换为 run.json 控制字段，禁止写入摘要正文。"""
        return {"candidate_id": self.candidate_id, "status": self.status.value, "session_baseline_version": self.session_baseline_version, "covered_through_conversation_id": self.covered_through_conversation_id, "source_hash": self.source_hash, "summary_hash": self.summary_hash, "context_version": self.context_version}


@dataclass(frozen=True)
class SuccessCommitIntent:
    """成功投影前持久化的恢复意图控制信息。"""

    conversation_id: str
    latest_candidate_id: str | None
    target_status: RunStatus
    run_id: str = ""
    session_id: str = ""

    def to_dict(self) -> JSONMap:
        """转换为 run.json 可恢复控制字段。"""
        return {"conversation_id": self.conversation_id, "latest_candidate_id": self.latest_candidate_id, "target_status": self.target_status.value, "run_id": self.run_id, "session_id": self.session_id}


def context_version_from_dict(data: JSONMap) -> ContextVersion:
    """从严格 v4 JSON 数据恢复不可变上下文版本。"""
    raw_slots: JSONValue | None = data.get("slots")
    if not isinstance(raw_slots, list):
        raise ValueError("context_versions.slots 必须是数组")
    slots: list[ContextSlotSnapshot] = []
    for raw_slot in raw_slots:
        slot_data: JSONMap = require_json_map(raw_slot)
        kind: ContextContributionKind = ContextContributionKind(get_string(slot_data, "contribution_kind"))
        mode: ContextPersistenceMode = ContextPersistenceMode(get_string(slot_data, "persistence_mode"))
        if mode is not ContextPersistenceMode.SNAPSHOT:
            raise ValueError("Context Version 不得持久化事实引用型 Slot")
        slots.append(ContextSlotSnapshot(
            slot_id=get_string(slot_data, "slot_id"), owner=ContextOwner(get_string(slot_data, "owner")),
            contribution_kind=kind, persistence_mode=mode, status=ContextSlotStatus(get_string(slot_data, "status")),
            injection_order=get_integer(slot_data, "injection_order"), content=_content_from_json(kind, slot_data.get("content")),
            content_hash=get_string(slot_data, "content_hash"), error_code=get_string(slot_data, "error_code"),
        ))
    result: ContextVersion = ContextVersion(get_integer(data, "version"), get_string(data, "created_at"), tuple(slots), get_string(data, "content_hash"), get_string(data, "tool_schema_hash"))
    _validate_context_version(result)
    return result


def new_context_version(version: int, slots: tuple[ContextSlotSnapshot, ...], content_hash: str, tool_schema_hash: str) -> ContextVersion:
    """构造并校验带统一时间戳的新上下文版本。"""
    result: ContextVersion = ContextVersion(version, utc_now_iso(), slots, content_hash, tool_schema_hash)
    _validate_context_version(result)
    return result


def _content_to_json(content: ContextSlotContent) -> JSONValue:
    """按 DTO 类型序列化直接 Slot 正文。"""
    if isinstance(content, TextSlotContent):
        return content.text
    if isinstance(content, ToolDefinitionsSlotContent):
        return [tool.to_dict() for tool in content.tools]
    if isinstance(content, ConversationMessagesSlotContent):
        return [message.to_dict() for message in content.messages]
    return list(content.message_ids)


def _content_from_json(kind: ContextContributionKind, value: JSONValue | None) -> ContextSlotContent:
    """按封闭 kind 严格反序列化 Slot 正文。"""
    if kind in {ContextContributionKind.SYSTEM_CONTENT, ContextContributionKind.HISTORY_COMPRESSIONS}:
        if not isinstance(value, str):
            raise ValueError("文本 Slot 的 content 必须是字符串")
        return TextSlotContent(value)
    if kind is ContextContributionKind.TOOL_DEFINITIONS:
        if not isinstance(value, list):
            raise ValueError("工具 Slot 的 content 必须是数组")
        tools: list[ToolDefinitionSlotContent] = []
        for raw_tool in value:
            tool: JSONMap = require_json_map(raw_tool)
            parameters: JSONValue | None = tool.get("parameters")
            if not isinstance(parameters, dict):
                raise ValueError("工具 Schema parameters 必须是对象")
            tools.append(ToolDefinitionSlotContent(get_string(tool, "name"), get_string(tool, "description"), parameters))
        return ToolDefinitionsSlotContent(tuple(tools))
    if kind is ContextContributionKind.CONVERSATION_MESSAGES:
        if not isinstance(value, list):
            raise ValueError("Conversation Slot 的 content 必须是数组")
        messages: list[ConversationSlotMessage] = []
        for raw_message in value:
            message: JSONMap = require_json_map(raw_message)
            messages.append(ConversationSlotMessage(get_string(message, "id"), MessageRole(get_string(message, "role")), get_string(message, "content"), get_string(message, "created_at")))
        return ConversationMessagesSlotContent(tuple(messages))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Run Message 引用 content 必须是字符串数组")
    return RunMessageReferencesSlotContent(tuple(value))


def _validate_context_version(context_version: ContextVersion) -> None:
    """校验版本编号、快照模式和有序注入序号。"""
    if context_version.version <= 0:
        raise ValueError("Context Version 必须从 1 开始")
    if any(slot.persistence_mode is not ContextPersistenceMode.SNAPSHOT for slot in context_version.slots):
        raise ValueError("Context Version 只能包含快照型 Slot")
    slot_ids: tuple[str, ...] = tuple(slot.slot_id for slot in context_version.slots)
    if len(slot_ids) != len(set(slot_ids)):
        raise ValueError("Context Version 内 Slot 标识必须唯一")
    orders: tuple[int, ...] = tuple(slot.injection_order for slot in context_version.slots)
    if orders != tuple(sorted(orders)):
        raise ValueError("Context Version 的 Slot 注入顺序必须递增")
