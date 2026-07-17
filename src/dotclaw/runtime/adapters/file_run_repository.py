"""Runtime v2 的本地文件 RunRepository 实现。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ..application.ports import ConversationProjectionPort
from ..domain.events import RunEvent
from ..domain.models import (
    AgentPolicySnapshot,
    AgentRun,
    ConversationMessage,
    JSONMap,
    JSONValue,
    MessageRole,
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
    utc_now_iso,
)
from ._file_support import (
    RunStorageFileName,
    SessionStorageFileName,
    StorageFormatVersion,
    load_json_map,
    validate_path_segment,
    write_json_atomic,
)


class FileRunRepository:
    """将运行摘要、消息、事件和 Conversation 投影写入本地目录。"""

    def __init__(
        self,
        root_directory: str | Path,
        conversation_projector: ConversationProjectionPort | None = None,
    ) -> None:
        """初始化仓储根目录和可选的既有 Session 投影器。"""
        self._root_directory: Path = Path(root_directory).resolve()
        self._conversation_projector: ConversationProjectionPort | None = conversation_projector

    async def create_run(self, run: AgentRun) -> None:
        """创建新的 run.json，禁止意外覆盖既有运行。"""
        await asyncio.to_thread(self._create_run_sync, run)

    async def load_run(self, session_id: str, run_id: str) -> AgentRun | None:
        """读取指定运行摘要；文件不存在时返回 None。"""
        return await asyncio.to_thread(self._load_run_sync, session_id, run_id)

    async def find_run(self, run_id: str) -> AgentRun | None:
        """跨 Session 定位运行摘要，供取消和审批恢复使用。"""
        return await asyncio.to_thread(self._find_run_sync, run_id)

    async def save_run(self, run: AgentRun) -> None:
        """原子更新已存在运行的摘要。"""
        await asyncio.to_thread(self._save_run_sync, run)

    async def save_messages(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        """原子保存完整消息，并保证消息序号与标识唯一。"""
        await asyncio.to_thread(self._save_messages_sync, session_id, run_id, messages)

    async def load_messages(self, session_id: str, run_id: str) -> tuple[RunMessage, ...]:
        """读取完整消息；尚未写入时返回空元组。"""
        return await asyncio.to_thread(self._load_messages_sync, session_id, run_id)

    async def append_event(self, session_id: str, event: RunEvent) -> None:
        """在消息已落盘后追加有序事件。"""
        await asyncio.to_thread(self._append_event_sync, session_id, event)

    async def commit_success(self, run: AgentRun, final_message: RunMessage) -> None:
        """写入完成摘要并投影最终 assistant 消息到 Conversation。"""
        user_message: RunMessage = await asyncio.to_thread(
            self._validate_success_and_get_input_sync,
            run,
            final_message,
        )
        await asyncio.to_thread(self._save_run_sync, run)
        if self._conversation_projector is not None:
            await self._conversation_projector.project_success(run, user_message, final_message)
            return
        await asyncio.to_thread(self._append_standalone_conversation_sync, run, final_message)

    async def load_conversation(self, session_id: str) -> tuple[ConversationMessage, ...]:
        """读取该仓储维护的成功 Conversation 投影。"""
        return await asyncio.to_thread(self._load_conversation_sync, session_id)

    def _create_run_sync(self, run: AgentRun) -> None:
        path: Path = self._run_path(run.session_id, run.run_id)
        if path.exists():
            raise FileExistsError(f"运行 {run.run_id} 已存在")
        write_json_atomic(path, run.to_dict())

    def _load_run_sync(self, session_id: str, run_id: str) -> AgentRun | None:
        path: Path = self._run_path(session_id, run_id)
        if not path.is_file():
            return None
        return _agent_run_from_dict(load_json_map(path))

    def _find_run_sync(self, run_id: str) -> AgentRun | None:
        """扫描受控运行目录并定位唯一 run.json。"""
        safe_run_id: str = validate_path_segment(run_id, "run_id")
        run_paths: tuple[Path, ...] = tuple(self._root_directory.glob(
            f"*/agent_runs/{safe_run_id}/{RunStorageFileName.RUN.value}"
        ))
        if len(run_paths) > 1:
            raise ValueError(f"运行 {run_id} 在多个 Session 中重复出现")
        if not run_paths:
            return None
        return _agent_run_from_dict(load_json_map(run_paths[0]))

    def _save_run_sync(self, run: AgentRun) -> None:
        path: Path = self._run_path(run.session_id, run.run_id)
        if not path.is_file():
            raise FileNotFoundError(f"运行 {run.run_id} 尚未创建")
        write_json_atomic(path, run.to_dict())

    def _save_messages_sync(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        self._validate_messages(messages)
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.MESSAGES.value)
        payload: JSONMap = {
            "run_id": run_id,
            "version": StorageFormatVersion.INITIAL,
            "messages": [message.to_dict() for message in messages],
        }
        write_json_atomic(path, payload)

    def _load_messages_sync(self, session_id: str, run_id: str) -> tuple[RunMessage, ...]:
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.MESSAGES.value)
        if not path.is_file():
            return ()
        payload: JSONMap = load_json_map(path)
        raw_messages: JSONValue | None = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("messages.json 的 messages 字段必须是数组")
        messages: list[RunMessage] = []
        raw_message: JSONValue
        for raw_message in raw_messages:
            messages.append(_run_message_from_dict(require_json_map(raw_message)))
        self._validate_messages(tuple(messages))
        return tuple(messages)

    def _append_event_sync(self, session_id: str, event: RunEvent) -> None:
        message_ids: frozenset[str] = frozenset(
            message.message_id for message in self._load_messages_sync(session_id, event.run_id)
        )
        missing_ids: tuple[str, ...] = tuple(
            message_id for message_id in event.message_ids if message_id not in message_ids
        )
        if missing_ids:
            raise ValueError(f"事件引用了尚未保存的消息：{', '.join(missing_ids)}")

        path: Path = self._run_path(session_id, event.run_id).with_name(RunStorageFileName.EVENTS.value)
        expected_sequence: int = self._next_event_sequence(path)
        if event.sequence != expected_sequence:
            raise ValueError(f"事件序号必须为 {expected_sequence}，实际为 {event.sequence}")
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized_event: str = json.dumps(event.to_dict(), ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as event_file:
            event_file.write(f"{serialized_event}\n")
            event_file.flush()
            os.fsync(event_file.fileno())

    def _validate_success_and_get_input_sync(self, run: AgentRun, final_message: RunMessage) -> RunMessage:
        """校验成功提交条件，并定位已持久化的用户输入消息。"""
        if run.status is not RunStatus.COMPLETED:
            raise ValueError("只有已完成的运行才能提交 Conversation 投影")
        if final_message.role is not MessageRole.ASSISTANT:
            raise ValueError("最终 Conversation 投影必须是 assistant 消息")
        if final_message.message_id != run.final_message_id:
            raise ValueError("最终消息必须与 AgentRun 摘要引用一致")
        messages: tuple[RunMessage, ...] = self._load_messages_sync(run.session_id, run.run_id)
        input_message: RunMessage | None = next(
            (message for message in messages if message.message_id == run.input_message_id),
            None,
        )
        if input_message is None:
            raise ValueError("成功运行必须引用已保存的用户输入消息")
        if input_message.kind is not RunMessageKind.USER_INPUT or input_message.role is not MessageRole.USER:
            raise ValueError("运行输入消息必须是 user_input 类型的用户消息")
        stored_final_message: RunMessage | None = next(
            (message for message in messages if message.message_id == final_message.message_id),
            None,
        )
        if stored_final_message != final_message:
            raise ValueError("最终消息必须先以完整 RunMessage 保存后才能提交成功投影")
        return input_message

    def _append_standalone_conversation_sync(self, run: AgentRun, final_message: RunMessage) -> None:
        """在未注入 Session 投影器时保留独立 Conversation 兼容容器。"""
        conversation_message: ConversationMessage = ConversationMessage(
            message_id=final_message.message_id,
            role=MessageRole.ASSISTANT,
            content=final_message.content,
            created_at=run.ended_at or utc_now_iso(),
        )
        self._append_conversation_message_sync(run.session_id, conversation_message)

    def _append_conversation_message_sync(self, session_id: str, message: ConversationMessage) -> None:
        path: Path = self._conversation_path(session_id)
        existing_messages: list[JSONMap] = []
        if path.is_file():
            existing_payload: JSONMap = load_json_map(path)
            raw_messages: JSONValue | None = existing_payload.get("messages")
            if isinstance(raw_messages, list):
                raw_message: JSONValue
                for raw_message in raw_messages:
                    existing_messages.append(require_json_map(raw_message))
        existing_messages.append(message.to_dict())
        payload: JSONMap = {
            "session_id": session_id,
            "version": StorageFormatVersion.INITIAL,
            "messages": existing_messages,
        }
        write_json_atomic(path, payload)

    def _load_conversation_sync(self, session_id: str) -> tuple[ConversationMessage, ...]:
        path: Path = self._conversation_path(session_id)
        if not path.is_file():
            return ()
        payload: JSONMap = load_json_map(path)
        raw_messages: JSONValue | None = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("conversation.json 的 messages 字段必须是数组")
        messages: list[ConversationMessage] = []
        raw_message: JSONValue
        for raw_message in raw_messages:
            messages.append(_conversation_message_from_dict(require_json_map(raw_message)))
        return tuple(messages)

    def _next_event_sequence(self, path: Path) -> int:
        if not path.is_file():
            return 1
        lines: list[str] = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return 1
        last_line: str = lines[-1]
        decoded_value: JSONValue = json.loads(last_line)
        last_event: JSONMap = require_json_map(decoded_value)
        return get_integer(last_event, "sequence") + 1

    def _run_path(self, session_id: str, run_id: str) -> Path:
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        safe_run_id: str = validate_path_segment(run_id, "run_id")
        return self._root_directory / safe_session_id / "agent_runs" / safe_run_id / RunStorageFileName.RUN.value

    def _conversation_path(self, session_id: str) -> Path:
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        return self._root_directory / safe_session_id / SessionStorageFileName.CONVERSATION.value

    def _validate_messages(self, messages: tuple[RunMessage, ...]) -> None:
        message_ids: set[str] = set()
        expected_sequence: int = 1
        message: RunMessage
        for message in messages:
            if message.sequence != expected_sequence:
                raise ValueError("运行消息序号必须从 1 连续递增")
            if message.message_id in message_ids:
                raise ValueError("运行消息标识必须唯一")
            message_ids.add(message.message_id)
            expected_sequence += 1


def _agent_run_from_dict(data: JSONMap) -> AgentRun:
    """将 run.json 反序列化为领域摘要。"""
    raw_policy: JSONValue | None = data.get("policy")
    raw_statistics: JSONValue | None = data.get("statistics")
    policy_data: JSONMap = require_json_map(raw_policy) if raw_policy is not None else {}
    statistics_data: JSONMap = require_json_map(raw_statistics) if raw_statistics is not None else {}
    raw_error: JSONValue | None = data.get("error")
    error: RunError | None = _run_error_from_dict(require_json_map(raw_error)) if raw_error is not None else None
    return AgentRun(
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
        agent_id=get_string(data, "agent_id"),
        status=RunStatus(get_string(data, "status", RunStatus.RUNNING.value)),
        started_at=get_string(data, "started_at"),
        policy=AgentPolicySnapshot(
            agent_id=get_string(policy_data, "agent_id"),
            identity_version=get_string(policy_data, "identity_version"),
            model_id=get_string(policy_data, "model_id"),
            max_iterations=get_integer(policy_data, "max_iterations"),
            policy_data=_json_map_or_empty(policy_data.get("policy_data")),
        ),
        input_message_id=get_string(data, "input_message_id"),
        parent_run_id=_optional_string(data.get("parent_run_id")),
        root_run_id=_optional_string(data.get("root_run_id")),
        ended_at=_optional_string(data.get("ended_at")),
        resume_count=get_integer(data, "resume_count"),
        final_message_id=_optional_string(data.get("final_message_id")),
        latest_checkpoint_id=_optional_string(data.get("latest_checkpoint_id")),
        statistics=RunStatistics(
            duration_ms=get_integer(statistics_data, "duration_ms"),
            llm_call_count=get_integer(statistics_data, "llm_call_count"),
            tool_call_count=get_integer(statistics_data, "tool_call_count"),
            tokens_in=get_integer(statistics_data, "tokens_in"),
            tokens_out=get_integer(statistics_data, "tokens_out"),
        ),
        error=error,
    )


def _run_message_from_dict(data: JSONMap) -> RunMessage:
    """将 messages.json 的单条记录反序列化为领域消息。"""
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


def _conversation_message_from_dict(data: JSONMap) -> ConversationMessage:
    """将 conversation.json 的单条记录反序列化为对话消息。"""
    return ConversationMessage(
        message_id=get_string(data, "id"),
        role=MessageRole(get_string(data, "role")),
        content=get_string(data, "content"),
        created_at=get_string(data, "created_at"),
    )


def _run_error_from_dict(data: JSONMap) -> RunError:
    """将 JSON 错误摘要反序列化为领域错误。"""
    raw_retryable: JSONValue | None = data.get("retryable")
    retryable: bool = raw_retryable if isinstance(raw_retryable, bool) else False
    return RunError(RunErrorCode(get_string(data, "code")), get_string(data, "message"), retryable)


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将可选 JSON 值收窄为对象，非对象时返回空对象。"""
    return value if isinstance(value, dict) else {}


def _optional_string(value: JSONValue | None) -> str | None:
    """将可选 JSON 值收窄为字符串或 None。"""
    return value if isinstance(value, str) else None
