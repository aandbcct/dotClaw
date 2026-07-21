"""Runtime E6 的 v4 RunRepository 格式与契约测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest

from dotclaw.runtime.adapters import CheckpointRepositoryAdapter, InMemoryRunRepository, RunRepositoryAdapter
from dotclaw.runtime.application.ports import RunRepository
from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextPersistenceMode,
    ContextSlotSnapshot,
    ContextSlotStatus,
    TextSlotContent,
    ContextVersion,
    StagedHistoryCompression,
    StagedHistoryCompressionStatus,
    SuccessCommitIntent,
    new_context_version,
)
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    JSONMap,
    MessageRole,
    RunCheckpoint,
    RunMessage,
    RunMessageKind,
    RunStatus,
    require_json_map,
)


class ContextVersionRepository(Protocol):
    """E1 Context Version 契约测试需要的最小仓储操作。"""

    async def create_run(self, run: AgentRun) -> None:
        """创建测试 Run。"""

    async def append_context_version(self, session_id: str, run_id: str, context_version: ContextVersion) -> None:
        """追加上下文版本。"""

    async def load_context_versions(self, session_id: str, run_id: str) -> tuple[ContextVersion, ...]:
        """读取上下文版本。"""


def _run() -> AgentRun:
    """构造 v4 仓储测试所需的最小 Run。"""
    return AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        status=RunStatus.RUNNING,
        started_at="2026-07-20T00:00:00+00:00",
        policy=AgentPolicySnapshot("agent-1", "identity-v1", "model-v1", 8),
        input_message_id="user-1",
    )


def _context_version(version: int) -> ContextVersion:
    """构造只包含稳定快照型 Slot 的完整版本。"""
    return new_context_version(
        version=version,
        slots=(
            ContextSlotSnapshot(
                slot_id="identity",
                owner=ContextOwner.AGENT,
                contribution_kind=ContextContributionKind.SYSTEM_CONTENT,
                persistence_mode=ContextPersistenceMode.SNAPSHOT,
                status=ContextSlotStatus.INCLUDED,
                injection_order=0,
                content=TextSlotContent("你是测试助手。"),
                content_hash="identity-hash",
            ),
            ContextSlotSnapshot(
                slot_id="history_compressions",
                owner=ContextOwner.SESSION,
                contribution_kind=ContextContributionKind.HISTORY_COMPRESSIONS,
                persistence_mode=ContextPersistenceMode.SNAPSHOT,
                status=ContextSlotStatus.EMPTY,
                injection_order=1,
                content=TextSlotContent(""),
            ),
        ),
        content_hash=f"context-hash-{version}",
        tool_schema_hash="tool-schema-hash",
    )


async def _assert_context_version_contract(repository: ContextVersionRepository) -> None:
    """验证 Fake 与文件仓储共享的追加不可变契约。"""
    run: AgentRun = _run()
    version_one: ContextVersion = _context_version(1)
    version_two: ContextVersion = _context_version(2)
    await repository.create_run(run)
    await repository.append_context_version(run.session_id, run.run_id, version_one)
    await repository.append_context_version(run.session_id, run.run_id, version_two)
    assert await repository.load_context_versions(run.session_id, run.run_id) == (version_one, version_two)
    with pytest.raises(ValueError, match="连续递增"):
        await repository.append_context_version(run.session_id, run.run_id, version_two)


async def test_in_memory_run_repository_satisfies_context_version_contract() -> None:
    """内存 Fake 必须与真实 Adapter 共享 v4 版本语义。"""
    await _assert_context_version_contract(InMemoryRunRepository())


async def test_file_run_repository_satisfies_context_version_contract(tmp_path: Path) -> None:
    """文件 Adapter 必须与内存 Fake 共享 v4 版本语义。"""
    await _assert_context_version_contract(RunRepositoryAdapter(tmp_path))


async def _assert_run_control_contract(repository: RunRepository) -> None:
    """验证活动版本、候选和成功意图均由 run.json 控制面保存。"""
    run: AgentRun = _run()
    candidate: StagedHistoryCompression = StagedHistoryCompression(
        candidate_id="candidate-1",
        status=StagedHistoryCompressionStatus.STAGED,
        session_baseline_version=1,
        covered_through_conversation_id="conversation-1",
        source_hash="source-hash",
        summary_hash="summary-hash",
        context_version=1,
    )
    intent: SuccessCommitIntent = SuccessCommitIntent(
        conversation_id="conversation-2",
        latest_candidate_id=candidate.candidate_id,
        target_status=RunStatus.COMPLETED,
    )
    await repository.create_run(run)
    await repository.append_context_version(run.session_id, run.run_id, _context_version(1))
    await repository.set_active_context_version(run.session_id, run.run_id, 1)
    await repository.save_staged_history_compressions(run.session_id, run.run_id, (candidate,))
    await repository.save_success_commit_intent(run.session_id, run.run_id, intent)
    persisted: AgentRun | None = await repository.load_run(run.session_id, run.run_id)
    assert persisted is not None
    assert persisted.active_context_version == 1
    assert persisted.staged_history_compressions == (candidate,)
    assert persisted.success_commit_intent == intent


async def test_in_memory_run_repository_satisfies_run_control_contract() -> None:
    """内存 Fake 必须保存与文件 Adapter 相同的控制面事实。"""
    await _assert_run_control_contract(InMemoryRunRepository())


async def test_file_run_repository_satisfies_run_control_contract(tmp_path: Path) -> None:
    """文件 Adapter 必须保存与内存 Fake 相同的控制面事实。"""
    await _assert_run_control_contract(RunRepositoryAdapter(tmp_path))


async def test_v4_messages_payload_keeps_context_versions_and_messages_separate(tmp_path: Path) -> None:
    """摘要候选正文不得进入 run.json，完整版本只写 messages.json。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run: AgentRun = _run()
    candidate: StagedHistoryCompression = StagedHistoryCompression(
        candidate_id="candidate-1",
        status=StagedHistoryCompressionStatus.STAGED,
        session_baseline_version=4,
        covered_through_conversation_id="conversation-3",
        source_hash="source-hash",
        summary_hash="summary-hash",
        context_version=1,
    )
    run = replace(run, staged_history_compressions=(candidate,))
    message: RunMessage = RunMessage("user-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "你好")
    await repository.create_run(run)
    await repository.save_messages(run.session_id, run.run_id, (message,))
    await repository.append_context_version(run.session_id, run.run_id, _context_version(1))

    run_payload: JSONMap = require_json_map(json.loads(
        (tmp_path / run.session_id / "agent_runs" / run.run_id / "run.json").read_text(encoding="utf-8"),
    ))
    messages_payload: JSONMap = require_json_map(json.loads(
        (tmp_path / run.session_id / "agent_runs" / run.run_id / "messages.json").read_text(encoding="utf-8"),
    ))
    assert run_payload["version"] == 4
    assert messages_payload["version"] == 4
    assert len(messages_payload["context_versions"]) == 1
    raw_candidates = run_payload["staged_history_compressions"]
    assert isinstance(raw_candidates, list)
    candidate_payload: JSONMap = require_json_map(raw_candidates[0])
    assert set(candidate_payload) == {
        "candidate_id",
        "status",
        "session_baseline_version",
        "covered_through_conversation_id",
        "source_hash",
        "summary_hash",
        "context_version",
    }
    assert set(messages_payload) == {"run_id", "version", "context_versions", "messages"}


async def test_v1_and_v2_messages_are_rejected_without_conversion(tmp_path: Path) -> None:
    """任何历史 messages.json 读取都必须明确失败，禁止隐式迁移。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run: AgentRun = _run()
    await repository.create_run(run)
    path: Path = tmp_path / run.session_id / "agent_runs" / run.run_id / "messages.json"
    for version in (1, 2, 3):
        path.write_text(json.dumps({"run_id": run.run_id, "version": version, "messages": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="仅支持 v4"):
            await repository.load_messages(run.session_id, run.run_id)


async def test_checkpoint_writes_and_reads_v4_control_fields(tmp_path: Path) -> None:
    """checkpoint.json 必须使用 v4，且只保存活动版本与候选引用。"""
    repository: CheckpointRepositoryAdapter = CheckpointRepositoryAdapter(tmp_path)
    checkpoint: RunCheckpoint = RunCheckpoint(
        checkpoint_id="checkpoint-1",
        run_id="run-1",
        session_id="session-1",
        checkpoint_sequence=1,
        event_sequence=2,
        message_sequence=3,
        agent_state={"phase": "waiting_llm"},
        next_action=AgentAction.INVOKE_LLM,
        pending={},
        budget={"max_iterations": 8},
        active_context_version=2,
        staged_history_compression_ids=("candidate-1",),
    )
    await repository.save(checkpoint)
    assert await repository.load(checkpoint.session_id, checkpoint.run_id) == checkpoint
    payload: JSONMap = require_json_map(json.loads(
        (tmp_path / checkpoint.session_id / "agent_runs" / checkpoint.run_id / "checkpoint.json").read_text(encoding="utf-8"),
    ))
    assert payload["version"] == 4
    assert payload["active_context_version"] == 2
    assert payload["staged_history_compression_ids"] == ["candidate-1"]


async def test_file_repository_uses_atomic_replacement_for_v4_payload(tmp_path: Path) -> None:
    """v4 多次写入后不得遗留临时文件，证明文件替换路径原子收口。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run: AgentRun = _run()
    await repository.create_run(run)
    await repository.save_messages(
        run.session_id,
        run.run_id,
        (RunMessage("user-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "第一条"),),
    )
    await repository.save_messages(
        run.session_id,
        run.run_id,
        (RunMessage("user-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "第二条"),),
    )
    assert not tuple(tmp_path.rglob("*.tmp"))
