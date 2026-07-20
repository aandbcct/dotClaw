"""将 Runtime v1 messages.json 显式迁移为初始快照与增量消息分离的 v2 格式。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from dotclaw.runtime.adapters._file_support import (
    RunStorageFileName,
    StorageFormatVersion,
    load_json_map,
    write_json_atomic,
)
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import (
    HistoryContextSnapshot,
    HistoryMessageSnapshot,
    InitialContextSnapshot,
    JSONMap,
    JSONValue,
    MessageRole,
    RunMessage,
    RunMessageKind,
    SystemContextSlot,
    SystemContextSlotScope,
    SystemContextSlotStatus,
    SystemContextSnapshot,
    ToolCall,
    get_integer,
    get_string,
    require_json_map,
    utc_now_iso,
)


class MessagesMigrationOutcome(StrEnum):
    """单个 messages.json 的迁移处理结果。"""

    MIGRATED = "migrated"
    ALREADY_CURRENT = "already_current"


@dataclass(frozen=True)
class MessagesMigrationReport:
    """v1 消息迁移后的可审计结果摘要。"""

    run_directory: Path
    outcome: MessagesMigrationOutcome
    original_message_count: int
    incremental_message_count: int
    removed_llm_request_count: int
    event_count: int
    events_rebuilt: bool
    messages_backup_path: Path | None
    events_backup_path: Path | None


def migrate_messages_v1_run(
    run_directory: Path,
    replace_backup: bool = False,
) -> MessagesMigrationReport:
    """迁移单个运行目录；v1 源文件会先备份，重复执行 v2 目录不产生副作用。"""
    resolved_run_directory: Path = run_directory.resolve()
    messages_path: Path = resolved_run_directory / RunStorageFileName.MESSAGES.value
    if not messages_path.is_file():
        raise FileNotFoundError(f"找不到 messages.json：{messages_path}")
    payload: JSONMap = load_json_map(messages_path)
    version: int = get_integer(payload, "version", int(StorageFormatVersion.INITIAL))
    if version == int(StorageFormatVersion.INITIAL_CONTEXT):
        return MessagesMigrationReport(
            run_directory=resolved_run_directory,
            outcome=MessagesMigrationOutcome.ALREADY_CURRENT,
            original_message_count=_message_count(payload),
            incremental_message_count=_message_count(payload),
            removed_llm_request_count=0,
            event_count=_event_line_count(resolved_run_directory),
            events_rebuilt=(
                resolved_run_directory / RunStorageFileName.EVENTS.value
            ).is_file(),
            messages_backup_path=None,
            events_backup_path=None,
        )
    if version != int(StorageFormatVersion.INITIAL):
        raise ValueError(f"不支持迁移的 messages.json 版本：{version}")

    run_id: str = get_string(payload, "run_id") or resolved_run_directory.name
    original_messages: tuple[RunMessage, ...] = _messages_from_payload(payload)
    events_path: Path = resolved_run_directory / RunStorageFileName.EVENTS.value
    original_events: tuple[RunEvent, ...] = _load_events(events_path)
    _require_safe_migration_input(original_messages, original_events)
    context_message_ids: frozenset[str] = _context_message_ids(original_events)
    initial_context: InitialContextSnapshot = _build_initial_context(
        resolved_run_directory,
        original_messages,
        original_events,
    )
    incremental_messages: tuple[RunMessage, ...] = _incremental_messages(
        original_messages,
        context_message_ids,
    )
    migrated_events: tuple[RunEvent, ...] = _migrate_events(
        resolved_run_directory,
        original_messages,
        incremental_messages,
        original_events,
        initial_context,
    )
    messages_backup_path: Path = _backup_file(messages_path, replace_backup)
    events_backup_path: Path | None = (
        _backup_file(events_path, replace_backup) if events_path.is_file() else None
    )
    migrated_payload: JSONMap = {
        "run_id": run_id,
        "version": int(StorageFormatVersion.INITIAL_CONTEXT),
        "initial_context": initial_context.to_dict(),
        "messages": [message.to_dict() for message in incremental_messages],
    }
    try:
        write_json_atomic(messages_path, migrated_payload)
        if events_path.is_file():
            _write_events(events_path, migrated_events)
    except Exception:
        _restore_backup(messages_backup_path, messages_path)
        if events_backup_path is not None:
            _restore_backup(events_backup_path, events_path)
        raise
    removed_count: int = sum(
        1 for message in original_messages if message.kind is RunMessageKind.LLM_REQUEST
    )
    return MessagesMigrationReport(
        run_directory=resolved_run_directory,
        outcome=MessagesMigrationOutcome.MIGRATED,
        original_message_count=len(original_messages),
        incremental_message_count=len(incremental_messages),
        removed_llm_request_count=removed_count,
        event_count=len(migrated_events),
        events_rebuilt=events_path.is_file(),
        messages_backup_path=messages_backup_path,
        events_backup_path=events_backup_path,
    )


def _message_count(payload: JSONMap) -> int:
    """返回合法消息数组的长度，格式异常时给出明确错误。"""
    raw_messages: JSONValue | None = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("messages.json 的 messages 字段必须是数组")
    return len(raw_messages)


def _messages_from_payload(payload: JSONMap) -> tuple[RunMessage, ...]:
    """严格反序列化 v1 消息，确保迁移输入不存在歧义。"""
    raw_messages: JSONValue | None = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("messages.json 的 messages 字段必须是数组")
    messages: list[RunMessage] = []
    raw_message: JSONValue
    for raw_message in raw_messages:
        messages.append(_run_message_from_dict(require_json_map(raw_message)))
    _validate_continuous_messages(tuple(messages))
    return tuple(messages)


def _require_safe_migration_input(
    messages: tuple[RunMessage, ...],
    events: tuple[RunEvent, ...],
) -> None:
    """拒绝缺失上下文索引的重复请求，避免把副本错误迁入增量事实。"""
    contains_llm_request: bool = any(
        message.kind is RunMessageKind.LLM_REQUEST for message in messages
    )
    has_context_built_event: bool = any(
        event.event_type is RunEventType.CONTEXT_BUILT for event in events
    )
    if contains_llm_request and not has_context_built_event:
        raise ValueError(
            "messages.json v1 含重复 LLM 请求但缺少 CONTEXT_BUILT 事件，无法安全迁移；请保留只读文件并人工处理",
        )


def _run_message_from_dict(data: JSONMap) -> RunMessage:
    """将历史持久化消息收敛为当前领域模型。"""
    raw_tool_calls: JSONValue | None = data.get("tool_calls")
    tool_calls: list[ToolCall] = []
    if isinstance(raw_tool_calls, list):
        raw_tool_call: JSONValue
        for raw_tool_call in raw_tool_calls:
            tool_call_data: JSONMap = require_json_map(raw_tool_call)
            tool_calls.append(ToolCall(
                call_id=get_string(tool_call_data, "call_id"),
                name=get_string(tool_call_data, "name"),
                arguments=_json_map_or_empty(tool_call_data.get("arguments")),
            ))
    return RunMessage(
        message_id=get_string(data, "id"),
        sequence=get_integer(data, "sequence"),
        kind=RunMessageKind(get_string(data, "kind")),
        role=MessageRole(get_string(data, "role")),
        content=get_string(data, "content"),
        tool_call_id=_optional_string(data.get("tool_call_id")),
        name=_optional_string(data.get("name")),
        tool_calls=tuple(tool_calls),
        metadata=_json_map_or_empty(data.get("metadata")),
    )


def _build_initial_context(
    run_directory: Path,
    messages: tuple[RunMessage, ...],
    events: tuple[RunEvent, ...],
) -> InitialContextSnapshot:
    """从首轮重复请求中提取可审计的兼容初始快照。"""
    first_request_messages: tuple[RunMessage, ...] = _first_context_messages(messages, events)
    system_contents: list[str] = [
        message.content
        for message in first_request_messages
        if message.role is MessageRole.SYSTEM
    ]
    rendered_system_content: str = "\n\n".join(system_contents)
    system_slot: SystemContextSlot = SystemContextSlot(
        name="legacy_v1_context",
        scope=SystemContextSlotScope.DYNAMIC,
        status=(
            SystemContextSlotStatus.INCLUDED
            if rendered_system_content
            else SystemContextSlotStatus.EMPTY
        ),
        content=rendered_system_content,
        content_hash=_hash_text(rendered_system_content) if rendered_system_content else "",
    )
    history_candidates: list[RunMessage] = [
        message
        for message in first_request_messages
        if message.role is not MessageRole.SYSTEM
    ]
    _remove_current_input(history_candidates, messages)
    recent_messages: tuple[HistoryMessageSnapshot, ...] = tuple(
        HistoryMessageSnapshot(
            conversation_id=f"legacy-history-{message.sequence}",
            role=message.role,
            content=message.content,
        )
        for message in history_candidates
    )
    history: HistoryContextSnapshot = HistoryContextSnapshot(
        source_session_id=run_directory.parent.parent.name,
        source_conversation_version=sum(
            1 for message in recent_messages if message.role is MessageRole.USER
        ),
        recent_messages=recent_messages,
        content_hash=_hash_json_value([message.to_dict() for message in recent_messages]),
    )
    return InitialContextSnapshot(
        system_context=SystemContextSnapshot(
            version=1,
            slot_order=(system_slot.name,),
            slots=(system_slot,),
            rendered_content_hash=_hash_text(rendered_system_content),
        ),
        history=history,
    )


def _first_context_messages(
    messages: tuple[RunMessage, ...],
    events: tuple[RunEvent, ...],
) -> tuple[RunMessage, ...]:
    """优先依据 CONTEXT_BUILT 还原首轮完整请求，缺失事件时退化为旧请求类型。"""
    messages_by_id: dict[str, RunMessage] = {
        message.message_id: message for message in messages
    }
    event: RunEvent
    for event in events:
        if event.event_type is RunEventType.CONTEXT_BUILT:
            return tuple(
                messages_by_id[message_id]
                for message_id in event.message_ids
                if message_id in messages_by_id
            )
    first_response_index: int = next(
        (
            index
            for index, message in enumerate(messages)
            if message.kind in {RunMessageKind.LLM_RESPONSE, RunMessageKind.FINAL_RESPONSE}
        ),
        len(messages),
    )
    return tuple(
        message
        for message in messages[:first_response_index]
        if message.kind is RunMessageKind.LLM_REQUEST
    )


def _remove_current_input(
    history_candidates: list[RunMessage],
    messages: tuple[RunMessage, ...],
) -> None:
    """移除首次重复请求中与已保存 user_input 相同的当前输入。"""
    current_input: RunMessage | None = next(
        (message for message in messages if message.kind is RunMessageKind.USER_INPUT),
        None,
    )
    if current_input is None:
        return
    index: int
    for index in range(len(history_candidates) - 1, -1, -1):
        candidate: RunMessage = history_candidates[index]
        if candidate.role is MessageRole.USER and candidate.content == current_input.content:
            del history_candidates[index]
            return


def _incremental_messages(
    messages: tuple[RunMessage, ...],
    context_message_ids: frozenset[str],
) -> tuple[RunMessage, ...]:
    """删除 CONTEXT_BUILT 标记的完整请求副本并重新编号真实运行事实。"""
    incremental: list[RunMessage] = []
    message: RunMessage
    for message in messages:
        if message.message_id in context_message_ids or message.kind is RunMessageKind.LLM_REQUEST:
            continue
        incremental.append(replace(message, sequence=len(incremental) + 1))
    _validate_continuous_messages(tuple(incremental))
    return tuple(incremental)


def _load_events(events_path: Path) -> tuple[RunEvent, ...]:
    """读取旧事件；没有 events.jsonl 的运行允许只迁移消息容器。"""
    if not events_path.is_file():
        return ()
    events: list[RunEvent] = []
    line: str
    for line in events_path.read_text(encoding="utf-8").splitlines():
        decoded_value: JSONValue = json.loads(line)
        events.append(_run_event_from_dict(require_json_map(decoded_value)))
    return tuple(events)


def _run_event_from_dict(data: JSONMap) -> RunEvent:
    """将历史事件转换为当前审计模型。"""
    raw_message_ids: JSONValue | None = data.get("message_ids")
    message_ids: tuple[str, ...] = tuple(
        value for value in raw_message_ids if isinstance(value, str)
    ) if isinstance(raw_message_ids, list) else ()
    return RunEvent(
        run_id=get_string(data, "run_id"),
        sequence=get_integer(data, "sequence"),
        event_type=RunEventType(get_string(data, "event_type")),
        occurred_at=get_string(data, "occurred_at") or utc_now_iso(),
        message_ids=message_ids,
        summary=get_string(data, "summary"),
        data=_json_map_or_empty(data.get("data")),
    )


def _migrate_events(
    run_directory: Path,
    original_messages: tuple[RunMessage, ...],
    incremental_messages: tuple[RunMessage, ...],
    events: tuple[RunEvent, ...],
    initial_context: InitialContextSnapshot,
) -> tuple[RunEvent, ...]:
    """删除旧 CONTEXT_BUILT 事件，并在每个模型完成事件前补齐 LLM_STARTED 审计。"""
    retained_ids: frozenset[str] = frozenset(
        message.message_id for message in incremental_messages
    )
    original_messages_by_id: dict[str, RunMessage] = {
        message.message_id: message for message in original_messages
    }
    migrated: list[RunEvent] = []
    context_messages: tuple[RunMessage, ...] = ()
    event: RunEvent
    for event in events:
        if event.event_type is RunEventType.CONTEXT_BUILT:
            context_messages = tuple(
                original_messages_by_id[message_id]
                for message_id in event.message_ids
                if message_id in original_messages_by_id
            )
            continue
        if event.event_type is RunEventType.LLM_COMPLETED:
            response_message: RunMessage = _response_for_event(event, incremental_messages)
            migrated.append(_inferred_llm_started_event(
                run_directory,
                incremental_messages,
                response_message,
                initial_context,
                context_messages,
                len(migrated) + 1,
                event.occurred_at,
            ))
        migrated.append(RunEvent(
            run_id=event.run_id,
            sequence=len(migrated) + 1,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            message_ids=tuple(
                message_id for message_id in event.message_ids if message_id in retained_ids
            ),
            summary=event.summary,
            data=event.data,
        ))
    return tuple(migrated)


def _response_for_event(
    event: RunEvent,
    messages: tuple[RunMessage, ...],
) -> RunMessage:
    """定位 LLM_COMPLETED 对应的已保留 assistant 回复。"""
    message_id: str
    for message_id in event.message_ids:
        message: RunMessage | None = next(
            (candidate for candidate in messages if candidate.message_id == message_id),
            None,
        )
        if message is not None and message.role is MessageRole.ASSISTANT:
            return message
    raise ValueError(f"LLM_COMPLETED 事件 {event.sequence} 缺少可迁移 assistant 回复")


def _inferred_llm_started_event(
    run_directory: Path,
    incremental_messages: tuple[RunMessage, ...],
    response_message: RunMessage,
    initial_context: InitialContextSnapshot,
    context_messages: tuple[RunMessage, ...],
    sequence: int,
    occurred_at: str,
) -> RunEvent:
    """根据旧重复请求推导不复制内容的 LLM_STARTED 审计事件。"""
    incremental_ids: tuple[str, ...] = tuple(
        message.message_id
        for message in incremental_messages
        if message.sequence < response_message.sequence
    )
    call_index: int = sum(
        1
        for message in incremental_messages
        if message.role is MessageRole.ASSISTANT and message.sequence <= response_message.sequence
    )
    history_version: int = initial_context.history.source_conversation_version
    return RunEvent(
        run_id=run_directory.name,
        sequence=sequence,
        event_type=RunEventType.LLM_STARTED,
        occurred_at=occurred_at,
        message_ids=incremental_ids,
        summary="由 v1 重复请求迁移推导的模型调用开始",
        data={
            "call_index": call_index,
            "model_id": _model_id(run_directory),
            "system_context_version": initial_context.system_context.version,
            "history_version": history_version,
            "incremental_message_ids": list(incremental_ids),
            "context_hash": _hash_json_value([message.to_dict() for message in context_messages]),
            "tool_schema_hash": _hash_json_value([]),
        },
    )


def _context_message_ids(events: tuple[RunEvent, ...]) -> frozenset[str]:
    """汇总旧 CONTEXT_BUILT 事件引用的完整请求副本标识。"""
    return frozenset(
        message_id
        for event in events
        if event.event_type is RunEventType.CONTEXT_BUILT
        for message_id in event.message_ids
    )


def _model_id(run_directory: Path) -> str:
    """从 run.json 提取模型标识，旧摘要缺失时保留空字符串。"""
    run_path: Path = run_directory / RunStorageFileName.RUN.value
    if not run_path.is_file():
        return ""
    payload: JSONMap = load_json_map(run_path)
    raw_policy: JSONValue | None = payload.get("policy")
    policy: JSONMap = raw_policy if isinstance(raw_policy, dict) else {}
    return get_string(policy, "model_id")


def _write_events(events_path: Path, events: tuple[RunEvent, ...]) -> None:
    """原子替换事件文件，确保新的序列连续且不引用已删除请求消息。"""
    serialized_events: str = "\n".join(
        json.dumps(event.to_dict(), ensure_ascii=False) for event in events
    )
    file_descriptor: int
    temporary_path_text: str
    file_descriptor, temporary_path_text = tempfile.mkstemp(
        prefix=f".{events_path.stem}.",
        suffix=".tmp",
        dir=events_path.parent,
    )
    temporary_path: Path = Path(temporary_path_text)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as temporary_file:
            temporary_file.write(f"{serialized_events}\n" if serialized_events else "")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(events_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _backup_file(path: Path, replace_backup: bool) -> Path:
    """为原始 v1 文件创建同目录备份，防止迁移工具覆盖唯一历史输入。"""
    backup_path: Path = path.with_name(f"{path.name}.v1.bak")
    if backup_path.exists() and not replace_backup:
        raise FileExistsError(f"迁移备份已存在：{backup_path}；如需替换请使用 --replace-backup")
    shutil.copy2(path, backup_path)
    return backup_path


def _restore_backup(backup_path: Path, destination_path: Path) -> None:
    """迁移双文件写入失败时从备份补偿原文件，恢复 v1 的完整可读状态。"""
    shutil.copy2(backup_path, destination_path)


def _event_line_count(run_directory: Path) -> int:
    """返回当前事件文件的行数，供已是 v2 的幂等报告使用。"""
    events_path: Path = run_directory / RunStorageFileName.EVENTS.value
    if not events_path.is_file():
        return 0
    return len(events_path.read_text(encoding="utf-8").splitlines())


def _validate_continuous_messages(messages: tuple[RunMessage, ...]) -> None:
    """校验消息标识唯一且序号连续，避免迁移后事件引用不稳定。"""
    message_ids: set[str] = set()
    expected_sequence: int = 1
    message: RunMessage
    for message in messages:
        if message.sequence != expected_sequence:
            raise ValueError("messages.json 消息序号必须从 1 连续递增")
        if not message.message_id or message.message_id in message_ids:
            raise ValueError("messages.json 消息标识必须非空且唯一")
        message_ids.add(message.message_id)
        expected_sequence += 1


def _hash_text(content: str) -> str:
    """计算稳定的 UTF-8 文本 hash。"""
    return sha256(content.encode("utf-8")).hexdigest()


def _hash_json_value(value: JSONValue | list[JSONMap]) -> str:
    """以稳定 JSON 表示计算审计 hash。"""
    serialized: str = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(serialized)


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将 JSON 值收窄为对象，非对象时返回空对象。"""
    return value if isinstance(value, dict) else {}


def _optional_string(value: JSONValue | None) -> str | None:
    """将 JSON 值收窄为字符串或空。"""
    return value if isinstance(value, str) else None


def _parse_arguments() -> argparse.Namespace:
    """解析单个运行目录的迁移命令参数。"""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="将 Runtime v1 messages.json 迁移为 v2 初始快照格式",
    )
    parser.add_argument("run_directory", type=Path, help="agent_runs/{run_id} 目录")
    parser.add_argument(
        "--replace-backup",
        action="store_true",
        help="允许替换同目录已有的 .v1.bak 备份",
    )
    return parser.parse_args()


def main() -> None:
    """执行迁移并输出可用于发布记录的摘要。"""
    arguments: argparse.Namespace = _parse_arguments()
    report: MessagesMigrationReport = migrate_messages_v1_run(
        arguments.run_directory,
        arguments.replace_backup,
    )
    print(
        f"messages 迁移结果={report.outcome.value}，run={report.run_directory.name}，"
        f"原消息={report.original_message_count}，增量消息={report.incremental_message_count}，"
        f"移除 llm_request={report.removed_llm_request_count}，事件={report.event_count}，"
        f"LLM 审计重建={report.events_rebuilt}",
    )


if __name__ == "__main__":
    main()
