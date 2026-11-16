"""Runtime v4 的本地文件 ApprovalRepository 实现。"""

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

    async def ensure(self, record: ApprovalRecord) -> None:
        """幂等投影审批记录，拒绝身份变化或决议翻转。"""
        await asyncio.to_thread(self._ensure_sync, record)

    async def load(self, approval_id: str) -> ApprovalRecord | None:
        """读取审批记录；不存在时返回 None。"""
        return await asyncio.to_thread(self._load_sync, approval_id)

    async def consume(self, approval_id: str) -> ApprovalRecord | None:
        """原子标记并返回仍处于待处理状态的审批记录。"""
        return await asyncio.to_thread(self._consume_sync, approval_id)

    async def delete_by_session(self, session_id: str) -> None:
        """删除该 Session 的全部审批记录，使其不可再恢复（开发计划阶段 5）。

        审批文件布局由本适配器独占负责，SessionManager 不直接了解；应用级删除
        协调流程据此清理待审批事实，避免遗留孤儿审批（总体设计 §5.2）。
        """
        await asyncio.to_thread(self._delete_by_session_sync, session_id)

    def _create_sync(self, record: ApprovalRecord) -> None:
        path: Path = self._approval_path(record.approval_id)
        if path.exists():
            raise FileExistsError(f"审批 {record.approval_id} 已存在")
        write_json_atomic(path, record.to_dict())

    def _ensure_sync(self, record: ApprovalRecord) -> None:
        path = self._approval_path(record.approval_id)
        current = self._load_sync(record.approval_id)
        if current is None:
            write_json_atomic(path, record.to_dict())
            return
        if current == record:
            return
        if (
            (current.approval_id, current.run_id, current.session_id) != (record.approval_id, record.run_id, record.session_id)
            or current.call_id not in {None, record.call_id}
            or (current.created_at, current.metadata) != (record.created_at, record.metadata)
        ):
            raise ValueError("审批投影身份或不可变字段冲突")
        if current.decision is not None and record.decision is not None:
            if current.status is record.status and current.decision is record.decision:
                return
            raise ValueError("审批决议已固定，不能改变状态或翻转决议")
        if current.status is ApprovalStatus.PENDING and (
            record.decision is not None or (current.call_id is None and record.call_id is not None)
        ):
            if record.decision is None and record.status is not ApprovalStatus.PENDING:
                raise ValueError("审批投影状态与待处理关联不一致")
            if record.decision is None:
                write_json_atomic(path, record.to_dict())
                return
            expected_status = ApprovalStatus.APPROVED if record.decision else ApprovalStatus.REJECTED
            if record.status not in {expected_status, ApprovalStatus.CONSUMED}:
                raise ValueError("审批投影状态与决议不一致")
            write_json_atomic(path, record.to_dict())
            return
        if current.status is ApprovalStatus.CONSUMED and current.decision is None and record.status is ApprovalStatus.CONSUMED and (
            record.decision is not None or (current.call_id is None and record.call_id is not None)
        ):
            write_json_atomic(path, record.to_dict())
            return
        raise ValueError("审批记录已存在相反或不同的投影事实")

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

    def _delete_by_session_sync(self, session_id: str) -> None:
        approvals_dir: Path = self._root_directory / "approvals"
        if not approvals_dir.is_dir():
            return
        for path in approvals_dir.glob("*.json"):
            record: ApprovalRecord = _approval_from_dict(load_json_map(path))
            if record.session_id == session_id:
                path.unlink()

    def _approval_path(self, approval_id: str) -> Path:
        safe_approval_id: str = validate_path_segment(approval_id, "approval_id")
        return self._root_directory / "approvals" / f"{safe_approval_id}.json"


def _approval_from_dict(data: JSONMap) -> ApprovalRecord:
    """将审批 JSON 反序列化为领域记录。"""
    raw_metadata: JSONValue | None = data.get("metadata")
    metadata: JSONMap = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_call_id = data.get("call_id")
    raw_decision = data.get("decision")
    raw_resolved_at = data.get("resolved_at")
    if raw_call_id is not None and not isinstance(raw_call_id, str):
        raise ValueError("approval.call_id 必须是字符串或 null")
    if raw_decision is not None and not isinstance(raw_decision, bool):
        raise ValueError("approval.decision 必须是布尔值或 null")
    if raw_resolved_at is not None and not isinstance(raw_resolved_at, str):
        raise ValueError("approval.resolved_at 必须是字符串或 null")
    return ApprovalRecord(
        approval_id=get_string(data, "approval_id"),
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
        status=ApprovalStatus(get_string(data, "status")),
        created_at=get_string(data, "created_at"),
        metadata=metadata,
        call_id=raw_call_id,
        decision=raw_decision,
        resolved_at=raw_resolved_at,
    )
