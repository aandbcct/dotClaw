"""Runtime v2 的本地文件 ApprovalRepository 实现。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from ..domain.facts import ApprovalRecord, ApprovalStatus, JSONMap, JSONValue, get_string
from ._file_support import load_json_map, validate_path_segment, write_json_atomic


class ApprovalRepositoryAdapter:
    """以 approval_id 定位运行并保证审批记录只消费一次的本地仓储适配器。"""

    def __init__(self, root_directory: str | Path) -> None:
        """初始化审批记录根目录。"""
        self._root_directory: Path = Path(root_directory).resolve()

    async def create(self, record: ApprovalRecord) -> None:
        """创建待处理审批记录。"""
        await asyncio.to_thread(self._create_sync, record)

    async def load(self, approval_id: str) -> ApprovalRecord | None:
        """读取审批记录；不存在时返回 None。"""
        return await asyncio.to_thread(self._load_sync, approval_id)

    async def consume(self, approval_id: str) -> ApprovalRecord | None:
        """原子标记并返回仍处于待处理状态的审批记录。"""
        return await asyncio.to_thread(self._consume_sync, approval_id)

    def _create_sync(self, record: ApprovalRecord) -> None:
        path: Path = self._approval_path(record.approval_id)
        if path.exists():
            raise FileExistsError(f"审批 {record.approval_id} 已存在")
        write_json_atomic(path, record.to_dict())

    def _load_sync(self, approval_id: str) -> ApprovalRecord | None:
        path: Path = self._approval_path(approval_id)
        if not path.is_file():
            return None
        return _approval_from_dict(load_json_map(path))

    def _consume_sync(self, approval_id: str) -> ApprovalRecord | None:
        record: ApprovalRecord | None = self._load_sync(approval_id)
        if record is None or record.status is not ApprovalStatus.PENDING:
            return None
        consumed_record: ApprovalRecord = replace(record, status=ApprovalStatus.CONSUMED)
        write_json_atomic(self._approval_path(approval_id), consumed_record.to_dict())
        return consumed_record

    def _approval_path(self, approval_id: str) -> Path:
        safe_approval_id: str = validate_path_segment(approval_id, "approval_id")
        return self._root_directory / "approvals" / f"{safe_approval_id}.json"


def _approval_from_dict(data: JSONMap) -> ApprovalRecord:
    """将审批 JSON 反序列化为领域记录。"""
    raw_metadata: JSONValue | None = data.get("metadata")
    metadata: JSONMap = raw_metadata if isinstance(raw_metadata, dict) else {}
    return ApprovalRecord(
        approval_id=get_string(data, "approval_id"),
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
        status=ApprovalStatus(get_string(data, "status")),
        created_at=get_string(data, "created_at"),
        metadata=metadata,
    )
