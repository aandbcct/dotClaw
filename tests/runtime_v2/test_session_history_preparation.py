"""创建 Run 前的 Session 历史压缩与冻结测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.runtime.application.context_compaction import ContextCompactionRequest, ContextCompactionResult
from dotclaw.runtime.application.dto import ConversationSnapshot
from dotclaw.runtime.application.session_history_preparation import (
    HistoryPreparationPolicy,
    SessionHistoryPreparationError,
    SessionHistoryPreparationService,
)
from dotclaw.runtime.domain.facts import ContextCompactionScope
from dotclaw.session.session import Session, SessionManager


class RecordingCompactor:
    """记录压缩输入并返回确定摘要的 ContextCompactionPort 替身。"""

    def __init__(self) -> None:
        self.request: ContextCompactionRequest | None = None

    async def compact(self, request: ContextCompactionRequest) -> ContextCompactionResult:
        """返回覆盖最后一个输入片段的第一版摘要。"""
        self.request = request
        return ContextCompactionResult(
            scope=request.scope,
            version=request.previous_summary_version + 1,
            covered_through_fragment_id=request.fragments[-1].fragment_id,
            content="已压缩的历史摘要",
            content_hash="summary-hash",
            source_hash="source-hash",
        )


class FailingCompactor:
    """模拟压缩模型失败，验证 Run 前准备不会损坏 Session。"""

    async def compact(self, request: ContextCompactionRequest) -> ContextCompactionResult:
        """始终拒绝生成摘要。"""
        raise RuntimeError("压缩模型不可用")


async def test_history_preparation_compacts_session_and_freezes_recent_snapshot(tmp_path: Path) -> None:
    """超出最近 Conversation 上限时写入压缩版本，并只冻结摘要后的近期历史。"""
    manager: SessionManager = SessionManager(tmp_path)
    session: Session = Session(id="session-history")
    session.add_conversation("问题一", "回答一", ["run-1"])
    session.add_conversation("问题二", "回答二", ["run-2"])
    session.add_conversation("问题三", "回答三", ["run-3"])
    await manager.save(session)
    compactor: RecordingCompactor = RecordingCompactor()
    service: SessionHistoryPreparationService = SessionHistoryPreparationService(
        manager,
        compactor,
        HistoryPreparationPolicy(max_context_tokens=100, max_recent_conversations=1, reserved_tokens=20),
    )

    snapshot: ConversationSnapshot = await service.prepare(session.id)

    assert compactor.request is not None
    assert compactor.request.scope is ContextCompactionScope.SESSION_HISTORY
    assert len(compactor.request.fragments) == 4
    assert snapshot.version == 3
    assert [message.role.value for message in snapshot.messages] == ["system", "user", "assistant"]
    assert snapshot.messages[0].content.endswith("已压缩的历史摘要")
    assert snapshot.messages[1].content == "问题三"
    persisted: Session | None = await manager.load(session.id)
    assert persisted is not None
    assert persisted.active_compression_version == 1
    assert persisted.history_compressions[0].covered_through_conversation_id == session.conversations[1].conversation_id
    assert len(persisted.conversations) == 3


async def test_history_preparation_rejects_run_when_compaction_fails(tmp_path: Path) -> None:
    """压缩失败时不保存新版本，调用方必须拒绝创建 Run。"""
    manager: SessionManager = SessionManager(tmp_path)
    session: Session = Session(id="session-history-failure")
    session.add_conversation("问题一", "回答一", ["run-1"])
    session.add_conversation("问题二", "回答二", ["run-2"])
    await manager.save(session)
    service: SessionHistoryPreparationService = SessionHistoryPreparationService(
        manager,
        FailingCompactor(),
        HistoryPreparationPolicy(max_context_tokens=100, max_recent_conversations=1, reserved_tokens=20),
    )

    with pytest.raises(RuntimeError, match="压缩模型不可用"):
        await service.prepare(session.id)

    persisted: Session | None = await manager.load(session.id)
    assert persisted is not None
    assert persisted.active_compression_version == 0
    assert persisted.history_compressions == []
