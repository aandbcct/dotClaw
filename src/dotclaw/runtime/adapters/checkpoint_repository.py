"""Runtime v3 的本地文件 CheckpointRepository 实现。"""

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
    _require_v3_format(data, "checkpoint.json")
    return RunCheckpoint(
        checkpoint_id=get_string(data, "checkpoint_id"),
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
        checkpoint_sequence=get_integer(data, "checkpoint_sequence"),
        event_sequence=get_integer(data, "event_sequence"),
        message_sequence=get_integer(data, "message_sequence"),
        agent_state=_json_map_or_empty(data.get("agent_state")),
        next_action=AgentAction(get_string(data, "next_action")),
        pending=_json_map_or_empty(data.get("pending")),
        budget=_json_map_or_empty(data.get("budget")),
        active_context_version=_optional_positive_integer(data.get("active_context_version")),
        staged_history_compression_ids=_string_tuple(data.get("staged_history_compression_ids")),
    )


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将 JSON 值收窄为对象，非对象时使用空对象。"""
    return value if isinstance(value, dict) else {}


def _validate_checkpoint_payload(checkpoint: RunCheckpoint) -> None:
    """确保检查点只保存最小控制状态，不夹带完整上下文或工具结果。"""
    _validate_json_value(checkpoint.agent_state, "agent_state")
    _validate_json_value(checkpoint.pending, "pending")
    _validate_json_value(checkpoint.budget, "budget")


def _require_v3_format(data: JSONMap, file_name: str) -> None:
    """拒绝 v1/v2 文件，避免隐式迁移产生第二套事实。"""
    version: int = get_integer(data, "version")
    if version != int(StorageFormatVersion.CONTEXT_VERSIONS):
        raise ValueError(f"不支持的 {file_name} 格式版本：{version}；仅支持 v3")


def _optional_positive_integer(value: JSONValue | None) -> int | None:
    """读取可选正整数版本号。"""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("active_context_version 必须为正整数或 null")
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
