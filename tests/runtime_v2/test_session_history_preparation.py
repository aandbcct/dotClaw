"""创建 Run 前的 Session 历史压缩与冻结测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dotclaw.runtime.application.context_compaction import ContextCompactionRequest, ContextCompactionResult
from dotclaw.runtime.application.dto import ConversationSnapshot
from dotclaw.runtime.application.session_history_preparation import (
    HistoryPreparationPolicy,
    SessionHistoryPreparationError,
    SessionHistoryPreparationService,
)
from dotclaw.runtime.domain.facts import ContextCompactionScope, JSONMap, JSONValue, require_json_map
from dotclaw.session.session import Session, SessionManager


class RecordingCompactor:
    """记录压缩输入并返回确定摘要的 ContextCompactionPort 替身。"""

    def __init__(self) -> None:
        self.request: ContextCompactionRequest | None = None
        self.requests: list[ContextCompactionRequest] = []

    async def compact(self, request: ContextCompactionRequest) -> ContextCompactionResult:
        """返回覆盖最后一个输入片段的第一版摘要。"""
        self.request = request
        self.requests.append(request)
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


async def test_history_preparation_rolls_compression_version_and_coverage(tmp_path: Path) -> None:
    """新增 Conversation 后再次压缩必须基于上一版摘要生成连续版本和覆盖边界。"""
    manager: SessionManager = SessionManager(tmp_path)
    session: Session = Session(id="session-history-rolling")
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

    await service.prepare(session.id)
    session_after_first_compression: Session | None = await manager.load(session.id)
    assert session_after_first_compression is not None
    session_after_first_compression.add_conversation("问题四", "回答四", ["run-4"])
    await manager.save(session_after_first_compression)
    snapshot: ConversationSnapshot = await service.prepare(session.id)

    persisted: Session | None = await manager.load(session.id)
    assert persisted is not None
    assert persisted.active_compression_version == 2
    assert [compression.version for compression in persisted.history_compressions] == [1, 2]
    assert persisted.history_compressions[1].previous_version == 1
    assert persisted.history_compressions[1].covered_through_conversation_id == session_after_first_compression.conversations[2].conversation_id
    assert len(compactor.requests) == 2
    assert compactor.requests[1].previous_summary_version == 1
    assert compactor.requests[1].previous_summary == "已压缩的历史摘要"
    assert [message.content for message in snapshot.messages] == [
        "以下是此前对话的压缩摘要：\n已压缩的历史摘要",
        "问题四",
        "回答四",
    ]


async def test_history_preparation_allows_different_sessions_to_prepare_concurrently(tmp_path: Path) -> None:
    """不同 Session 的历史压缩互不覆盖，可并行生成各自的摘要版本。"""
    manager: SessionManager = SessionManager(tmp_path)
    first_session: Session = Session(id="session-history-parallel-1")
    second_session: Session = Session(id="session-history-parallel-2")
    session: Session
    for session in (first_session, second_session):
        session.add_conversation("问题一", "回答一", ["run-1"])
        session.add_conversation("问题二", "回答二", ["run-2"])
        await manager.save(session)
    compactor: RecordingCompactor = RecordingCompactor()
    service: SessionHistoryPreparationService = SessionHistoryPreparationService(
        manager,
        compactor,
        HistoryPreparationPolicy(max_context_tokens=100, max_recent_conversations=1, reserved_tokens=20),
    )

    first_snapshot: ConversationSnapshot
    second_snapshot: ConversationSnapshot
    first_snapshot, second_snapshot = await asyncio.gather(
        service.prepare(first_session.id),
        service.prepare(second_session.id),
    )

    assert first_snapshot.session_id == first_session.id
    assert second_snapshot.session_id == second_session.id
    first_persisted: Session | None = await manager.load(first_session.id)
    second_persisted: Session | None = await manager.load(second_session.id)
    assert first_persisted is not None
    assert second_persisted is not None
    assert first_persisted.active_compression_version == 1
    assert second_persisted.active_compression_version == 1
    assert len(compactor.requests) == 2


async def test_legacy_session_load_assigns_stable_conversation_ids_before_save(tmp_path: Path) -> None:
    """缺少 conversation_id 的旧 Session 必须在读取后稳定补齐，并可安全进入压缩边界。"""
    manager: SessionManager = SessionManager(tmp_path)
    session_id: str = "session-history-legacy"
    session_path: Path = tmp_path / session_id / "session.json"
    session_path.parent.mkdir(parents=True)
    legacy_payload: JSONMap = {
        "id": session_id,
        "title": "旧会话",
        "conversations": [{
            "user_query": "旧问题",
            "final_answer": "旧回答",
            "agent_run_ids": ["run-legacy"],
            "created_at": "2026-07-19T00:00:00+00:00",
        }],
    }
    session_path.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

    loaded: Session | None = await manager.load(session_id)

    assert loaded is not None
    assigned_conversation_id: str = loaded.conversations[0].conversation_id
    assert assigned_conversation_id.startswith("legacy-")
    await manager.save(loaded)
    persisted_payload: JSONMap = require_json_map(json.loads(session_path.read_text(encoding="utf-8")))
    raw_conversations: JSONValue | None = persisted_payload.get("conversations")
    assert isinstance(raw_conversations, list)
    persisted_conversation: JSONMap = require_json_map(raw_conversations[0])
    assert persisted_conversation["conversation_id"] == assigned_conversation_id
