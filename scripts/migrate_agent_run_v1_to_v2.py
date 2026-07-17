"""将旧版单文件 AgentRun 迁移为 Runtime v2 运行容器。"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from dotclaw.runtime.adapters import FileCheckpointRepository, FileRunRepository
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.models import (
    AgentAction,
    AgentPolicySnapshot,
    AgentRun,
    JSONMap,
    JSONValue,
    MessageRole,
    RunCheckpoint,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    RunStatistics,
    RunStatus,
    ToolCall,
    get_integer,
    get_string,
    require_json_map,
)


class LegacyRunStatus(StrEnum):
    """旧 AgentRun 文件中出现的终态值。"""

    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class MigrationReport:
    """单个旧 AgentRun 的迁移结果摘要。"""

    source_path: Path
    destination_path: Path
    run_id: str
    message_count: int
    event_count: int
    checkpoint_created: bool
    legacy_trace_id_count: int


async def migrate_agent_run(
    source_path: Path,
    destination_root: Path,
    session_id: str,
    overwrite: bool = False,
) -> MigrationReport:
    """迁移一个旧 AgentRun 文件，源文件保持只读不被修改。"""
    source_data: JSONMap = _load_legacy_run(source_path)
    run_id: str = get_string(source_data, "run_id")
    if not run_id:
        raise ValueError("旧 AgentRun 缺少 run_id")

    destination_path: Path = destination_root / session_id / "agent_runs" / run_id
    if destination_path.exists():
        if not overwrite:
            raise FileExistsError(f"迁移目标已存在：{destination_path}")
        shutil.rmtree(destination_path)

    status: RunStatus = _map_legacy_status(get_string(source_data, "end_status"))
    messages: tuple[RunMessage, ...] = _convert_legacy_messages(source_data, status)
    input_message_id: str = _find_input_message_id(messages, run_id)
    final_message_id: str | None = _find_final_message_id(messages)
    error: RunError | None = _convert_legacy_error(source_data)
    policy: AgentPolicySnapshot = AgentPolicySnapshot(
        agent_id=get_string(source_data, "agent_id"),
        identity_version="legacy-v1",
        model_id="",
        max_iterations=_legacy_max_iterations(source_data),
    )
    run: AgentRun = AgentRun(
        run_id=run_id,
        session_id=session_id,
        agent_id=policy.agent_id,
        status=status,
        started_at=get_string(source_data, "started_at"),
        ended_at=_optional_string(source_data.get("ended_at")),
        policy=policy,
        input_message_id=input_message_id,
        parent_run_id=_optional_string(source_data.get("parent_run_id")),
        root_run_id=None,
        final_message_id=final_message_id,
        statistics=RunStatistics(
            duration_ms=get_integer(source_data, "duration_ms"),
            llm_call_count=_legacy_llm_call_count(source_data),
            tool_call_count=get_integer(source_data, "tool_calls"),
            tokens_in=get_integer(source_data, "tokens_in"),
            tokens_out=get_integer(source_data, "tokens_out"),
        ),
        error=error,
    )
    run_repository: FileRunRepository = FileRunRepository(destination_root)
    checkpoint_repository: FileCheckpointRepository = FileCheckpointRepository(destination_root)
    await run_repository.create_run(run)
    await run_repository.save_messages(session_id, run_id, messages)

    events: tuple[RunEvent, ...] = _build_migration_events(run, messages)
    event: RunEvent
    for event in events:
        await run_repository.append_event(session_id, event)

    checkpoint_created: bool = False
    checkpoint: RunCheckpoint | None = _build_checkpoint(run, source_data, len(events), len(messages))
    if checkpoint is not None:
        await checkpoint_repository.save(checkpoint)
        checkpoint_created = True
        run = replace(run, latest_checkpoint_id=checkpoint.checkpoint_id)
        await run_repository.save_run(run)

    if run.status is RunStatus.COMPLETED and final_message_id is not None:
        final_message: RunMessage = _find_message(messages, final_message_id)
        completed_event: RunEvent = _find_completed_event(events)
        await run_repository.commit_success(run, final_message, completed_event)

    legacy_trace_ids: JSONValue | None = source_data.get("trace_ids")
    trace_id_count: int = len(legacy_trace_ids) if isinstance(legacy_trace_ids, list) else 0
    return MigrationReport(
        source_path=source_path,
        destination_path=destination_path,
        run_id=run_id,
        message_count=len(messages),
        event_count=len(events),
        checkpoint_created=checkpoint_created,
        legacy_trace_id_count=trace_id_count,
    )


def _load_legacy_run(source_path: Path) -> JSONMap:
    """读取并校验旧单文件 AgentRun JSON。"""
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到旧 AgentRun 文件：{source_path}")
    raw_text: str = source_path.read_text(encoding="utf-8")
    decoded_value: JSONValue = json.loads(raw_text)
    return require_json_map(decoded_value)


def _map_legacy_status(value: str) -> RunStatus:
    """将旧终态映射为 Runtime v2 的运行状态。"""
    legacy_status: LegacyRunStatus
    try:
        legacy_status = LegacyRunStatus(value)
    except ValueError:
        legacy_status = LegacyRunStatus.FAILED
    mapping: dict[LegacyRunStatus, RunStatus] = {
        LegacyRunStatus.COMPLETED: RunStatus.COMPLETED,
        LegacyRunStatus.FAILED: RunStatus.FAILED,
        LegacyRunStatus.WAITING: RunStatus.WAITING_APPROVAL,
        LegacyRunStatus.HANDOFF: RunStatus.COMPLETED,
    }
    return mapping[legacy_status]


def _convert_legacy_messages(source_data: JSONMap, status: RunStatus) -> tuple[RunMessage, ...]:
    """将旧 messages 字段转换为独立 RunMessage 容器。"""
    raw_messages: JSONValue | None = source_data.get("messages")
    if not isinstance(raw_messages, list):
        return ()
    run_id: str = get_string(source_data, "run_id")
    messages: list[RunMessage] = []
    raw_message: JSONValue
    for sequence, raw_message in enumerate(raw_messages, start=1):
        message_data: JSONMap = require_json_map(raw_message)
        role: MessageRole = _legacy_message_role(get_string(message_data, "role"))
        content: str = _legacy_message_content(message_data)
        message: RunMessage = RunMessage(
            message_id=f"{run_id}-message-{sequence}",
            sequence=sequence,
            kind=_message_kind(role),
            role=role,
            content=content,
            tool_call_id=_optional_string(message_data.get("tool_call_id")),
            name=_optional_string(message_data.get("name")),
            tool_calls=_legacy_tool_calls(message_data),
        )
        messages.append(message)
    if status is RunStatus.COMPLETED:
        final_index: int | None = _last_final_assistant_index(messages)
        if final_index is not None:
            messages[final_index] = replace(messages[final_index], kind=RunMessageKind.FINAL_RESPONSE)
    return tuple(messages)


def _legacy_message_role(value: str) -> MessageRole:
    """将旧角色字符串收敛为领域角色枚举。"""
    try:
        return MessageRole(value)
    except ValueError:
        return MessageRole.ASSISTANT


def _legacy_message_content(data: JSONMap) -> str:
    """兼容旧 system 的 content_lines 与其他角色的 content。"""
    raw_content_lines: JSONValue | None = data.get("content_lines")
    if isinstance(raw_content_lines, list):
        lines: list[str] = []
        raw_line: JSONValue
        for raw_line in raw_content_lines:
            if isinstance(raw_line, str):
                lines.append(raw_line)
        return "\n".join(lines)
    return get_string(data, "content")


def _legacy_tool_calls(data: JSONMap) -> tuple[ToolCall, ...]:
    """将旧工具调用数组转换为具有 JSON 参数对象的领域工具调用。"""
    raw_tool_calls: JSONValue | None = data.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return ()
    tool_calls: list[ToolCall] = []
    raw_tool_call: JSONValue
    for raw_tool_call in raw_tool_calls:
        tool_call_data: JSONMap = require_json_map(raw_tool_call)
        tool_calls.append(ToolCall(
            call_id=get_string(tool_call_data, "id"),
            name=get_string(tool_call_data, "name"),
            arguments=_legacy_tool_arguments(tool_call_data.get("arguments")),
        ))
    return tuple(tool_calls)


def _legacy_tool_arguments(value: JSONValue | None) -> JSONMap:
    """解析旧工具参数字符串，不能解析时保留原文。"""
    if not isinstance(value, str):
        return {}
    try:
        decoded_value: JSONValue = json.loads(value)
    except json.JSONDecodeError:
        return {"raw_arguments": value}
    return decoded_value if isinstance(decoded_value, dict) else {"raw_arguments": value}


def _message_kind(role: MessageRole) -> RunMessageKind:
    """按角色分配默认运行消息用途。"""
    mapping: dict[MessageRole, RunMessageKind] = {
        MessageRole.SYSTEM: RunMessageKind.LLM_REQUEST,
        MessageRole.USER: RunMessageKind.USER_INPUT,
        MessageRole.ASSISTANT: RunMessageKind.LLM_RESPONSE,
        MessageRole.TOOL: RunMessageKind.TOOL_RESULT,
    }
    return mapping[role]


def _last_final_assistant_index(messages: list[RunMessage]) -> int | None:
    """定位成功运行中的最后一条非工具调用 assistant 消息。"""
    index: int
    for index in range(len(messages) - 1, -1, -1):
        message: RunMessage = messages[index]
        if message.role is MessageRole.ASSISTANT and not message.tool_calls:
            return index
    return None


def _find_input_message_id(messages: tuple[RunMessage, ...], run_id: str) -> str:
    """优先返回第一条用户消息标识。"""
    message: RunMessage
    for message in messages:
        if message.role is MessageRole.USER:
            return message.message_id
    return f"{run_id}-input"


def _find_final_message_id(messages: tuple[RunMessage, ...]) -> str | None:
    """返回被标记为最终回复的消息标识。"""
    message: RunMessage
    for message in messages:
        if message.kind is RunMessageKind.FINAL_RESPONSE:
            return message.message_id
    return None


def _find_message(messages: tuple[RunMessage, ...], message_id: str) -> RunMessage:
    """按消息标识返回已确认存在的消息。"""
    message: RunMessage
    for message in messages:
        if message.message_id == message_id:
            return message
    raise ValueError(f"找不到最终消息：{message_id}")


def _find_completed_event(events: tuple[RunEvent, ...]) -> RunEvent:
    """返回迁移阶段已持久化的唯一 RUN_COMPLETED 事件。"""
    event: RunEvent
    for event in events:
        if event.event_type is RunEventType.RUN_COMPLETED:
            return event
    raise ValueError("已完成旧运行缺少 RUN_COMPLETED 迁移事件")


def _legacy_max_iterations(source_data: JSONMap) -> int:
    """从旧 state_snapshot 读取最大迭代次数。"""
    raw_snapshot: JSONValue | None = source_data.get("state_snapshot")
    snapshot: JSONMap = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    return get_integer(snapshot, "max_iterations", 10)


def _legacy_llm_call_count(source_data: JSONMap) -> int:
    """从旧状态快照的 iteration 字段近似恢复模型调用次数。"""
    raw_snapshot: JSONValue | None = source_data.get("state_snapshot")
    snapshot: JSONMap = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    return get_integer(snapshot, "iteration")


def _convert_legacy_error(source_data: JSONMap) -> RunError | None:
    """将旧错误字符串收敛为标准错误摘要。"""
    raw_error: JSONValue | None = source_data.get("error")
    if not isinstance(raw_error, str) or not raw_error:
        return None
    return RunError(RunErrorCode.INVALID_STATE, raw_error)


def _build_migration_events(run: AgentRun, messages: tuple[RunMessage, ...]) -> tuple[RunEvent, ...]:
    """根据旧摘要构造最小且连续的 v2 审计事件。"""
    events: list[RunEvent] = [RunEvent(
        run_id=run.run_id,
        sequence=1,
        event_type=RunEventType.RUN_STARTED,
        occurred_at=run.started_at,
        message_ids=(run.input_message_id,) if messages else (),
        summary="由旧 AgentRun 迁移创建",
    )]
    terminal_event_type: RunEventType = _terminal_event_type(run.status)
    terminal_message_ids: tuple[str, ...] = (run.final_message_id,) if run.final_message_id is not None else ()
    events.append(RunEvent(
        run_id=run.run_id,
        sequence=2,
        event_type=terminal_event_type,
        occurred_at=run.ended_at or run.started_at,
        message_ids=terminal_message_ids,
        summary="由旧 AgentRun 终态迁移创建",
    ))
    return tuple(events)


def _terminal_event_type(status: RunStatus) -> RunEventType:
    """将运行终态映射为对应的审计事件类型。"""
    mapping: dict[RunStatus, RunEventType] = {
        RunStatus.RUNNING: RunEventType.RUN_FAILED,
        RunStatus.COMPLETED: RunEventType.RUN_COMPLETED,
        RunStatus.FAILED: RunEventType.RUN_FAILED,
        RunStatus.CANCELLED: RunEventType.RUN_CANCELLED,
        RunStatus.WAITING_APPROVAL: RunEventType.WAITING_APPROVAL,
    }
    return mapping[status]


def _build_checkpoint(
    run: AgentRun,
    source_data: JSONMap,
    event_count: int,
    message_count: int,
) -> RunCheckpoint | None:
    """仅将旧状态的最小控制字段迁入 checkpoint。"""
    raw_snapshot: JSONValue | None = source_data.get("state_snapshot")
    if not isinstance(raw_snapshot, dict):
        return None
    agent_state: JSONMap = _safe_agent_state(raw_snapshot)
    next_action: AgentAction = AgentAction.WAIT if run.status is RunStatus.WAITING_APPROVAL else AgentAction.FINALIZE
    pending: JSONMap = {"kind": "approval"} if run.status is RunStatus.WAITING_APPROVAL else {}
    return RunCheckpoint(
        checkpoint_id=f"{run.run_id}-legacy-checkpoint",
        run_id=run.run_id,
        session_id=run.session_id,
        checkpoint_sequence=1,
        event_sequence=event_count,
        message_sequence=message_count,
        agent_state=agent_state,
        next_action=next_action,
        pending=pending,
        budget={
            "max_iterations": run.policy.max_iterations,
            "tokens_in": run.statistics.tokens_in,
            "tokens_out": run.statistics.tokens_out,
        },
    )


def _safe_agent_state(snapshot: JSONMap) -> JSONMap:
    """筛选旧 state_snapshot，避免将 prompt 或工具结果复制进 checkpoint。"""
    allowed_fields: tuple[str, ...] = (
        "phase",
        "iteration",
        "max_iterations",
        "end_status",
        "error_message",
        "handoff_target",
        "tool_calls_total",
        "truncated_count",
        "retry_count",
    )
    safe_state: JSONMap = {}
    field_name: str
    for field_name in allowed_fields:
        value: JSONValue | None = snapshot.get(field_name)
        if value is not None:
            safe_state[field_name] = value
    return safe_state


def _optional_string(value: JSONValue | None) -> str | None:
    """将可选 JSON 值收窄为字符串或 None。"""
    return value if isinstance(value, str) else None


def _parse_arguments() -> argparse.Namespace:
    """解析命令行迁移参数。"""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="迁移旧 AgentRun 到 Runtime v2 文件容器")
    parser.add_argument("source", type=Path, help="旧 agent_runs/{run_id}.json 文件")
    parser.add_argument("--destination-root", type=Path, default=None, help="v2 sessions 根目录，默认沿用旧文件所在根目录")
    parser.add_argument("--session-id", default=None, help="所属 Session ID，默认从旧文件路径推导")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同 run_id 的既有迁移目录")
    return parser.parse_args()


def _default_destination_root(source_path: Path) -> Path:
    """根据旧 agent_runs/{run}.json 路径推导 sessions 根目录。"""
    return source_path.parents[2]


def _default_session_id(source_path: Path) -> str:
    """根据旧 agent_runs/{run}.json 路径推导 Session ID。"""
    return source_path.parents[1].name


async def _main_async() -> None:
    """执行命令行迁移并输出简洁结果。"""
    arguments: argparse.Namespace = _parse_arguments()
    source_path: Path = arguments.source.resolve()
    destination_root: Path = arguments.destination_root or _default_destination_root(source_path)
    session_id: str = arguments.session_id or _default_session_id(source_path)
    report: MigrationReport = await migrate_agent_run(source_path, destination_root, session_id, arguments.overwrite)
    print(
        f"已迁移 run={report.run_id}，消息={report.message_count}，事件={report.event_count}，"
        f"checkpoint={report.checkpoint_created}，旧 trace_ids={report.legacy_trace_id_count}",
    )


if __name__ == "__main__":
    asyncio.run(_main_async())
