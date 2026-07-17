"""审批记录创建与消费服务。"""

from __future__ import annotations

import uuid

from ..domain.facts import ApprovalRecord, ApprovalStatus, utc_now_iso
from .ports import ApprovalRepository


class ApprovalService:
    """将审批 ID 与等待中的 Run 建立唯一且可消费的关联。"""

    def __init__(self, repository: ApprovalRepository) -> None:
        """绑定审批持久化端口。"""
        self._repository: ApprovalRepository = repository

    async def create(self, run_id: str, session_id: str, approval_id: str | None) -> ApprovalRecord:
        """创建待处理审批记录；工具未指定 ID 时生成新的标识。"""
        record: ApprovalRecord = ApprovalRecord(
            approval_id=approval_id or uuid.uuid4().hex,
            run_id=run_id,
            session_id=session_id,
            status=ApprovalStatus.PENDING,
            created_at=utc_now_iso(),
        )
        await self._repository.create(record)
        return record

    async def find_pending(self, approval_id: str) -> ApprovalRecord | None:
        """读取待处理审批记录，仅用于协调器定位对应 Session 租约。"""
        record: ApprovalRecord | None = await self._repository.load(approval_id)
        if record is None or record.status is not ApprovalStatus.PENDING:
            return None
        return record

    async def consume(self, approval_id: str) -> ApprovalRecord | None:
        """原子消费审批记录，防止重复恢复同一 Run。"""
        return await self._repository.consume(approval_id)
