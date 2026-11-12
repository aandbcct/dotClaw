"""Runtime v4 的本地文件 CheckpointRepository 实现。"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path

from ..domain.control import AgentAction
from ..domain.facts import JSONMap, JSONValue, RunCheckpoint, get_integer, get_string
from ._file_support import StorageFormatVersion
from ._file_support import RunStorageFileName, load_json_map, validate_path_segment, write_json_atomic


class CheckpointForbiddenField(StrEnum):
    """不得写入恢复检查点的完整上下文载荷字段。"""

    PROMPT = "prompt"
    FULL_PROMPT = "full_prompt"
    MESSAGES = "messages"
    TOOL_RESULT = "tool_result"
    TOOL_RESULTS = "tool_results"


FORBIDDEN_CHECKPOINT_FIELD_NAMES: frozenset[str] = frozenset(
    field.value for field in CheckpointForbiddenField
)
"""检查点中禁止出现的完整上下文载荷字段名。"""


class CheckpointRepositoryAdapter:
    """按 session_id/run_id 保存最新恢复检查点的本地仓储适配器。"""

    def __init__(self, root_directory: str | Path) -> None:
        """初始化检查点根目录。"""
        self._root_directory: Path = Path(root_directory).resolve()

    async def save(self, checkpoint: RunCheckpoint) -> None:
        """原子保存最新检查点。"""
        await asyncio.to_thread(self._save_sync, checkpoint)

    async def load(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        """加载检查点；不存在时返回 None。"""
        return await asyncio.to_thread(self._load_sync, session_id, run_id)

    async def delete(self, session_id: str, run_id: str) -> None:
        """删除完成或取消后不再需要的检查点。"""
        await asyncio.to_thread(self._delete_sync, session_id, run_id)

    def _save_sync(self, checkpoint: RunCheckpoint) -> None:
        _validate_checkpoint_payload(checkpoint)
        path: Path = self._checkpoint_path(checkpoint.session_id, checkpoint.run_id)
        payload: JSONMap = checkpoint.to_dict()
        payload["version"] = int(StorageFormatVersion.CONTEXT_VERSIONS)
        write_json_atomic(path, payload)

    def _load_sync(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        path: Path = self._checkpoint_path(session_id, run_id)
        if not path.is_file():
            return None
        checkpoint: RunCheckpoint = _checkpoint_from_dict(load_json_map(path))
        _validate_checkpoint_payload(checkpoint)
        return checkpoint

    def _delete_sync(self, session_id: str, run_id: str) -> None:
        path: Path = self._checkpoint_path(session_id, run_id)
        if path.is_file():
            path.unlink()

    def _checkpoint_path(self, session_id: str, run_id: str) -> Path:
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        safe_run_id: str = validate_path_segment(run_id, "run_id")
        return self._root_directory / safe_session_id / "agent_runs" / safe_run_id / RunStorageFileName.CHECKPOINT.value


def _checkpoint_from_dict(data: JSONMap) -> RunCheckpoint:
    """将 checkpoint.json 反序列化为领域检查点。"""
    _require_v4_format(data, "checkpoint.json")
    action: AgentAction = AgentAction(get_string(data, "action", AgentAction.INVOKE_LLM.value))
    return RunCheckpoint(
        checkpoint_id=get_string(data, "checkpoint_id"),
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
        checkpoint_sequence=get_integer(data, "checkpoint_sequence"),
        event_sequence=get_integer(data, "event_sequence"),
        message_sequence=get_integer(data, "message_sequence"),
        action=action,
        pending=_json_map_or_empty(data.get("pending")),
        budget=_json_map_or_empty(data.get("budget")),
        active_context_version=_optional_positive_integer(data.get("active_context_version")),
        staged_history_compression_ids=_string_tuple(data.get("staged_history_compression_ids")),
        tool_ledger_revision=_optional_positive_integer(data.get("tool_ledger_revision")),
        replay_context=_optional_bool(data.get("replay_context"), "replay_context"),
    )


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将 JSON 值收窄为对象，非对象时使用空对象。"""
    return value if isinstance(value, dict) else {}


def _validate_checkpoint_payload(checkpoint: RunCheckpoint) -> None:
    """确保检查点只保存最小控制状态，不夹带完整上下文或工具结果。"""
    pending = dict(checkpoint.pending)
    has_new_tool_fields = "tool_batch_id" in pending or "pending_call_ids" in pending
    if has_new_tool_fields:
        if "tool_calls" in pending:
            raise ValueError("新格式 pending 不得与旧 tool_calls 同时存在")
        if not isinstance(pending.get("tool_batch_id"), str) or not pending["tool_batch_id"]:
            raise ValueError("新格式 pending 必须包含非空 tool_batch_id")
        call_ids = pending.get("pending_call_ids")
        if not isinstance(call_ids, list) or not all(isinstance(call_id, str) and call_id for call_id in call_ids):
            raise ValueError("pending_call_ids 必须是字符串数组")
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("pending_call_ids 不得重复")
        for key in ("approval_id", "child_run_id"):
            if key in pending and (not isinstance(pending[key], str) or not pending[key]):
                raise ValueError(f"pending.{key} 必须是非空字符串")
    if checkpoint.tool_ledger_revision is not None and (
        not isinstance(checkpoint.tool_ledger_revision, int)
        or isinstance(checkpoint.tool_ledger_revision, bool)
        or checkpoint.tool_ledger_revision <= 0
    ):
        raise ValueError("tool_ledger_revision 必须是正整数或 null")
    if checkpoint.replay_context is not None and not isinstance(checkpoint.replay_context, bool):
        raise ValueError("replay_context 必须是布尔值或 null")
    prepared = pending.pop("prepared_request", None)
    if prepared is not None:
        if pending:
            raise ValueError("准备检查点只能保存冻结请求")
        _validate_prepared_request(checkpoint, prepared)
    _validate_json_value(pending, "pending")
    _validate_json_value(checkpoint.budget, "budget")


def _validate_prepared_request(checkpoint: RunCheckpoint, value: JSONValue) -> None:
    """准备期仅允许固定结构的冻结输入；Created 状态由 Engine 保存入口校验。"""
    expected = {"session_id", "lease_id", "agent_id", "user_message", "conversation",
                "parent_run_id", "root_run_id", "run_id", "model_id", "delegation_task_id"}
    if (checkpoint.action is not AgentAction.INVOKE_LLM or checkpoint.active_context_version is not None
            or not isinstance(value, dict) or set(value) != expected
            or value.get("run_id") != checkpoint.run_id or value.get("session_id") != checkpoint.session_id):
        raise ValueError("准备请求结构或运行归属不一致")
    for key in ("session_id", "lease_id", "agent_id", "run_id", "model_id"):
        if not isinstance(value[key], str):
            raise ValueError("准备请求标识必须为字符串")
    for key in ("parent_run_id", "root_run_id", "delegation_task_id"):
        if value[key] is not None and not isinstance(value[key], str):
            raise ValueError("准备请求可选标识必须为字符串或空")
    conversation = value["conversation"]
    if (not isinstance(conversation, dict)
            or set(conversation) != {"session_id", "messages", "version", "compressed_history"}
            or conversation["session_id"] != checkpoint.session_id
            or not isinstance(conversation["version"], int)
            or isinstance(conversation["version"], bool)
            or not isinstance(conversation["messages"], list)):
        raise ValueError("准备请求会话结构或归属不一致")
    for message in [value["user_message"], *conversation["messages"]]:
        if (not isinstance(message, dict) or set(message) != {"id", "role", "content", "created_at"}
                or not all(isinstance(item, str) for item in message.values())
                or message["role"] not in {"user", "assistant", "system", "tool"}):
            raise ValueError("准备请求消息必须为固定输入结构")
    history = conversation["compressed_history"]
    if history is not None and (not isinstance(history, dict)
            or set(history) != {"compression_version", "covered_through_conversation_id", "content", "content_hash"}
            or not isinstance(history["compression_version"], int) or isinstance(history["compression_version"], bool)
            or history["compression_version"] <= 0
            or any(not isinstance(history[key], str) for key in ("covered_through_conversation_id", "content", "content_hash"))):
        raise ValueError("准备请求压缩历史必须为固定输入结构")


def _require_v4_format(data: JSONMap, file_name: str) -> None:
    """拒绝 v1/v2 文件，避免隐式迁移产生第二套事实。"""
    version: int = get_integer(data, "version")
    if version != int(StorageFormatVersion.CONTEXT_VERSIONS):
        raise ValueError(f"不支持的 {file_name} 格式版本：{version}；仅支持 v4")


def _optional_positive_integer(value: JSONValue | None) -> int | None:
    """读取可选正整数版本号。"""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("active_context_version 必须为正整数或 null")
    return value


def _optional_bool(value: JSONValue | None, field_name: str) -> bool | None:
    """读取可选布尔控制字段，不把整数当作布尔值。"""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是布尔值或 null")
    return value


def _string_tuple(value: JSONValue | None) -> tuple[str, ...]:
    """读取严格字符串数组。"""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("staged_history_compression_ids 必须是字符串数组")
    return tuple(value)


def _validate_json_value(value: JSONValue, path: str) -> None:
    """递归检查 JSON 字段名，阻止禁止的完整数据载荷。"""
    if isinstance(value, dict):
        field_name: str
        nested_value: JSONValue
        for field_name, nested_value in value.items():
            normalized_name: str = field_name.lower()
            if normalized_name in FORBIDDEN_CHECKPOINT_FIELD_NAMES:
                raise ValueError(f"checkpoint 不得保存完整上下文载荷：{path}.{field_name}")
            _validate_json_value(nested_value, f"{path}.{field_name}")
        return
    if isinstance(value, list):
        index: int
        item: JSONValue
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
