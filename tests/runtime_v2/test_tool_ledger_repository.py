"""工具账本与幂等仓储投影的文件/内存静态契约样本。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dotclaw.eval.environment import InMemoryCheckpointRepository
from dotclaw.eval.fixtures import FixtureApprovalRepository
from dotclaw.runtime.adapters import CheckpointRepositoryAdapter, InMemoryRunRepository, RunRepositoryAdapter
from dotclaw.runtime.adapters._file_support import RunStorageFileName
from dotclaw.runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from dotclaw.runtime.application.ports import ApprovalRepository, CheckpointRepository, RunRepository
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    ApprovalRecord,
    ApprovalStatus,
    MessageRole,
    RunCheckpoint,
    RunMessage,
    RunMessageKind,
)
from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running
from dotclaw.runtime.domain.tool_recovery import ToolCallLedger
from dotclaw.eval.models import FixtureMatchMode


def _run() -> AgentRun:
    return AgentRun(
        "run-1", "session-1", "agent-1", AgentRunState(Running(RunStage.CALLING_LLM)),
        "2026-11-12T00:00:00+00:00", AgentPolicySnapshot("agent-1", "v1", "model", 5), "input-1",
    )


def _checkpoint(sequence: int) -> RunCheckpoint:
    return RunCheckpoint(
        f"checkpoint-{sequence}", "run-1", "session-1", sequence, sequence, sequence,
        {}, {}, tool_ledger_revision=sequence, replay_context=False,
    )


def _pending_approval(approval_id: str = "approval-1") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id, "run-1", "session-1", ApprovalStatus.PENDING, "2026-11-12T00:00:00+00:00",
        {"tool": "lookup"}, call_id="call-1",
    )


async def _assert_run_repository_contract(repository: RunRepository) -> None:
    run = _run()
    await repository.create_run(run)
    assert await repository.load_tool_ledger(run.session_id, run.run_id) is None
    first = ToolCallLedger(run.session_id, run.run_id, 1, ())
    await repository.save_tool_ledger(first, expected_revision=0)
    assert await repository.load_tool_ledger(run.session_id, run.run_id) == first
    second = replace(first, revision=2)
    await repository.save_tool_ledger(second, expected_revision=1)
    with pytest.raises(ValueError, match="revision"):
        await repository.save_tool_ledger(replace(second, revision=3), expected_revision=1)
    assert await repository.load_tool_ledger(run.session_id, run.run_id) == second
    with pytest.raises(ValueError, match="恰好递增"):
        await repository.save_tool_ledger(replace(second, revision=4), expected_revision=2)

    first_message = RunMessage("message-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "input")
    second_message = RunMessage("message-2", 2, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "result")
    await repository.ensure_tool_messages(run.session_id, run.run_id, (first_message,))
    await repository.ensure_tool_messages(run.session_id, run.run_id, (first_message,))
    with pytest.raises(ValueError, match="完整内容冲突"):
        await repository.ensure_tool_messages(run.session_id, run.run_id, (replace(first_message, content="different"),))
    assert await repository.load_messages(run.session_id, run.run_id) == (first_message,)
    with pytest.raises(ValueError, match="序号必须连续"):
        await repository.ensure_tool_messages(run.session_id, run.run_id, (replace(second_message, sequence=3),))
    await repository.ensure_tool_messages(run.session_id, run.run_id, (second_message,))

    first_event = RunEvent(run.run_id, 1, RunEventType.TOOL_STARTED, "2026-11-12T00:00:01+00:00", ("message-1",), "start")
    second_event = RunEvent(run.run_id, 2, RunEventType.TOOL_COMPLETED, "2026-11-12T00:00:02+00:00", ("message-2",), "done")
    await repository.ensure_tool_events(run.session_id, (first_event,))
    await repository.ensure_tool_events(run.session_id, (first_event,))
    with pytest.raises(ValueError, match="冲突"):
        await repository.ensure_tool_events(run.session_id, (replace(first_event, summary="different"),))
    assert await repository.load_events(run.session_id, run.run_id) == (first_event,)
    with pytest.raises(ValueError, match="序号必须连续"):
        await repository.ensure_tool_events(run.session_id, (replace(second_event, sequence=3),))
    await repository.ensure_tool_events(run.session_id, (second_event,))
    assert await repository.load_events(run.session_id, run.run_id) == (first_event, second_event)

    target_run = replace(run, resume_count=1)
    await repository.ensure_tool_run(run, target_run)
    await repository.ensure_tool_run(run, target_run)
    with pytest.raises(ValueError, match="基线已变化"):
        await repository.ensure_tool_run(run, replace(run, resume_count=2))
    assert await repository.load_run(run.session_id, run.run_id) == target_run


async def test_file_and_memory_run_repositories_share_tool_projection_contract(tmp_path: Path) -> None:
    await _assert_run_repository_contract(InMemoryRunRepository())
    await _assert_run_repository_contract(RunRepositoryAdapter(tmp_path / "file"))


async def test_file_ledger_rejects_corruption_and_wrong_storage_identity(tmp_path: Path) -> None:
    repository = RunRepositoryAdapter(tmp_path)
    run = _run()
    await repository.create_run(run)
    path = tmp_path / run.session_id / "agent_runs" / run.run_id / RunStorageFileName.TOOL_CALLS.value
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        await repository.load_tool_ledger(run.session_id, run.run_id)
    path.write_text(json.dumps(ToolCallLedger("other-session", run.run_id, 1, ()).to_dict()), encoding="utf-8")
    with pytest.raises(ValueError, match="存储路径"):
        await repository.load_tool_ledger(run.session_id, run.run_id)


async def test_file_event_projection_refuses_damage_without_rewriting_old_bytes(tmp_path: Path) -> None:
    repository = RunRepositoryAdapter(tmp_path)
    run = _run()
    await repository.create_run(run)
    path = tmp_path / run.session_id / "agent_runs" / run.run_id / RunStorageFileName.EVENTS.value
    original = '{"partial":\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        await repository.ensure_tool_events(
            run.session_id,
            (RunEvent(run.run_id, 1, RunEventType.RUN_STARTED, "2026-11-12T00:00:00+00:00"),),
        )
    assert path.read_text(encoding="utf-8") == original


async def _assert_checkpoint_repository_contract(repository: CheckpointRepository) -> None:
    first, second, third = _checkpoint(1), _checkpoint(2), _checkpoint(3)
    invalid = replace(first, pending={"tool_calls": [], "tool_batch_id": "batch-1", "pending_call_ids": []})
    with pytest.raises(ValueError, match="不得与旧 tool_calls"):
        await repository.ensure_tool_checkpoint(None, invalid)
    assert await repository.load(first.session_id, first.run_id) is None
    await repository.ensure_tool_checkpoint(None, first)
    await repository.ensure_tool_checkpoint(None, first)
    await repository.ensure_tool_checkpoint(first, second)
    with pytest.raises(ValueError, match="基线已变化"):
        await repository.ensure_tool_checkpoint(None, third)
    assert await repository.load(second.session_id, second.run_id) == second
    await repository.ensure_tool_checkpoint(second, None)
    await repository.ensure_tool_checkpoint(second, None)
    with pytest.raises(ValueError, match="基线已变化"):
        await repository.ensure_tool_checkpoint(first, third)


async def test_file_and_memory_checkpoint_repositories_share_base_target_contract(tmp_path: Path) -> None:
    await _assert_checkpoint_repository_contract(CheckpointRepositoryAdapter(tmp_path / "file"))
    await _assert_checkpoint_repository_contract(InMemoryCheckpointRepository())


async def _assert_approval_repository_contract(repository: ApprovalRepository) -> None:
    pending = _pending_approval()
    approved = replace(pending, status=ApprovalStatus.APPROVED, decision=True, resolved_at="2026-11-12T00:00:01+00:00")
    await repository.ensure(pending)
    await repository.ensure(pending)
    await repository.ensure(approved)
    await repository.ensure(approved)
    await repository.ensure(replace(approved, resolved_at="2026-11-12T00:00:03+00:00"))
    with pytest.raises(ValueError, match="不同的投影"):
        await repository.ensure(pending)
    rejected = replace(approved, status=ApprovalStatus.REJECTED, decision=False)
    with pytest.raises(ValueError):
        await repository.ensure(rejected)
    assert await repository.load("approval-1") == approved
    with pytest.raises(ValueError, match="身份"):
        await repository.ensure(replace(approved, session_id="other-session"))

    for decision in (True, False):
        pending_consumed = _pending_approval(f"approval-consumed-{decision}")
        consumed = replace(
            pending_consumed,
            status=ApprovalStatus.CONSUMED,
            decision=decision,
            resolved_at="2026-11-12T00:00:04+00:00",
        )
        await repository.ensure(pending_consumed)
        await repository.ensure(consumed)
        await repository.ensure(consumed)
        await repository.ensure(replace(consumed, resolved_at="2026-11-12T00:00:05+00:00"))
        assert await repository.load(consumed.approval_id) == consumed
        with pytest.raises(ValueError):
            await repository.ensure(replace(consumed, decision=not decision))

    legacy = ApprovalRecord("approval-legacy", "run-1", "session-1", ApprovalStatus.CONSUMED, "2026-11-12T00:00:00+00:00")
    await repository.ensure(legacy)
    with pytest.raises(ValueError):
        await repository.ensure(replace(legacy, status=ApprovalStatus.APPROVED, call_id="call-1", decision=True, resolved_at="2026-11-12T00:00:02+00:00"))


async def test_file_and_fixture_memory_approval_repositories_share_ensure_contract(tmp_path: Path) -> None:
    await _assert_approval_repository_contract(ApprovalRepositoryAdapter(tmp_path / "file"))
    await _assert_approval_repository_contract(FixtureApprovalRepository((), FixtureMatchMode.STRICT))
