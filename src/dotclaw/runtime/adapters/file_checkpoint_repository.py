"""Runtime v2 的本地文件 CheckpointRepository 实现。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..domain.models import AgentAction, JSONMap, JSONValue, RunCheckpoint, get_integer, get_string
from ._file_support import RunStorageFileName, load_json_map, validate_path_segment, write_json_atomic


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
        path: Path = self._checkpoint_path(checkpoint.session_id, checkpoint.run_id)
        write_json_atomic(path, checkpoint.to_dict())

    def _load_sync(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        path: Path = self._checkpoint_path(session_id, run_id)
        if not path.is_file():
            return None
        return _checkpoint_from_dict(load_json_map(path))

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
