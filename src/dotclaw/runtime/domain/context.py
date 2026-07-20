"""Runtime v3 上下文版本与提交控制面的领域事实。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .facts import JSONMap, JSONValue, RunStatus, get_integer, get_string, require_json_map, utc_now_iso


class ContextContributionKind(StrEnum):
    """上下文槽对模型输入贡献的数据形态。"""

    SYSTEM_CONTENT = "system_content"
    HISTORY = "history"
    RUN_MESSAGE_REFERENCES = "run_message_references"


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


@dataclass(frozen=True)
class ContextSlotSnapshot:
    """一个已绑定 Slot 的不可变、可审计载荷快照。"""

    slot_id: str
    owner: ContextOwner
    contribution_kind: ContextContributionKind
    status: ContextSlotStatus
    injection_order: int
    content: str = ""
    content_hash: str = ""
    message_ids: tuple[str, ...] = ()
    attributes: JSONMap = field(default_factory=dict)
    error_code: str = ""

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的稳定 Slot 记录。"""
        return {
            "slot_id": self.slot_id,
            "owner": self.owner.value,
            "contribution_kind": self.contribution_kind.value,
            "status": self.status.value,
            "injection_order": self.injection_order,
            "content": self.content,
            "content_hash": self.content_hash,
            "message_ids": list(self.message_ids),
            "attributes": self.attributes,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class ContextVersion:
    """一次业务模型调用前形成的完整、不可变上下文版本。"""

    version: int
    created_at: str
    slots: tuple[ContextSlotSnapshot, ...]
    content_hash: str
    tool_schema_hash: str

    def to_dict(self) -> JSONMap:
        """转换为 messages.json 的上下文版本记录。"""
        return {
            "version": self.version,
            "created_at": self.created_at,
            "slots": [slot.to_dict() for slot in self.slots],
            "content_hash": self.content_hash,
            "tool_schema_hash": self.tool_schema_hash,
        }


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
        return {
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "session_baseline_version": self.session_baseline_version,
            "covered_through_conversation_id": self.covered_through_conversation_id,
            "source_hash": self.source_hash,
            "summary_hash": self.summary_hash,
            "context_version": self.context_version,
        }


@dataclass(frozen=True)
class SuccessCommitIntent:
    """成功投影前持久化的恢复意图控制信息。"""

    conversation_id: str
    latest_candidate_id: str | None
    target_status: RunStatus

    def to_dict(self) -> JSONMap:
        """转换为 run.json 可恢复控制字段。"""
        return {
            "conversation_id": self.conversation_id,
            "latest_candidate_id": self.latest_candidate_id,
            "target_status": self.target_status.value,
        }


def context_version_from_dict(data: JSONMap) -> ContextVersion:
    """从严格 JSON 数据恢复不可变上下文版本。"""
    raw_slots: JSONValue | None = data.get("slots")
    if not isinstance(raw_slots, list):
        raise ValueError("context_versions.slots 必须是数组")
    slots: list[ContextSlotSnapshot] = []
    raw_slot: JSONValue
    for raw_slot in raw_slots:
        slot_data: JSONMap = require_json_map(raw_slot)
        raw_message_ids: JSONValue | None = slot_data.get("message_ids")
        if not isinstance(raw_message_ids, list) or not all(isinstance(value, str) for value in raw_message_ids):
            raise ValueError("Context Slot 的 message_ids 必须是字符串数组")
        slots.append(ContextSlotSnapshot(
            slot_id=get_string(slot_data, "slot_id"),
            owner=ContextOwner(get_string(slot_data, "owner")),
            contribution_kind=ContextContributionKind(get_string(slot_data, "contribution_kind")),
            status=ContextSlotStatus(get_string(slot_data, "status")),
            injection_order=get_integer(slot_data, "injection_order"),
            content=get_string(slot_data, "content"),
            content_hash=get_string(slot_data, "content_hash"),
            message_ids=tuple(raw_message_ids),
            attributes=_json_map_or_empty(slot_data.get("attributes")),
            error_code=get_string(slot_data, "error_code"),
        ))
    result: ContextVersion = ContextVersion(
        version=get_integer(data, "version"),
        created_at=get_string(data, "created_at"),
        slots=tuple(slots),
        content_hash=get_string(data, "content_hash"),
        tool_schema_hash=get_string(data, "tool_schema_hash"),
    )
    _validate_context_version(result)
    return result


def new_context_version(
    version: int,
    slots: tuple[ContextSlotSnapshot, ...],
    content_hash: str,
    tool_schema_hash: str,
) -> ContextVersion:
    """构造并校验带统一时间戳的新上下文版本。"""
    result: ContextVersion = ContextVersion(version, utc_now_iso(), slots, content_hash, tool_schema_hash)
    _validate_context_version(result)
    return result


def _validate_context_version(context_version: ContextVersion) -> None:
    """校验版本编号、Slot 标识和有序注入序号。"""
    if context_version.version <= 0:
        raise ValueError("Context Version 必须从 1 开始")
    slot_ids: tuple[str, ...] = tuple(slot.slot_id for slot in context_version.slots)
    if len(slot_ids) != len(set(slot_ids)):
        raise ValueError("Context Version 内 Slot 标识必须唯一")
    injection_orders: tuple[int, ...] = tuple(slot.injection_order for slot in context_version.slots)
    if injection_orders != tuple(sorted(injection_orders)):
        raise ValueError("Context Version 的 Slot 注入顺序必须递增")


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将可选 JSON 值收窄为精确对象。"""
    return value if isinstance(value, dict) else {}
