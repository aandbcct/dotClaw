"""Runtime v2 的本地文件 CheckpointRepository 实现。"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path

from ..domain.models import AgentAction, JSONMap, JSONValue, RunCheckpoint, get_integer, get_string
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


class FileCheckpointRepository:
    """按 session_id/run_id 保存最新恢复检查点。"""

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
        write_json_atomic(path, checkpoint.to_dict())

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
    )


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将 JSON 值收窄为对象，非对象时使用空对象。"""
    return value if isinstance(value, dict) else {}


def _validate_checkpoint_payload(checkpoint: RunCheckpoint) -> None:
    """确保检查点只保存最小控制状态，不夹带完整上下文或工具结果。"""
    _validate_json_value(checkpoint.agent_state, "agent_state")
    _validate_json_value(checkpoint.pending, "pending")
    _validate_json_value(checkpoint.budget, "budget")


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
