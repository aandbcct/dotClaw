"""Runtime v2 文件仓储的原子写入与引用完整性测试。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from scripts.migrate_agent_run_v1_to_v2 import migrate_agent_run
from dotclaw.runtime.adapters import (
    FileApprovalRepository,
    FileCheckpointRepository,
    FileRunRepository,
    SessionConversationProjector,
)
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.models import (
    AgentAction,
    AgentPolicySnapshot,
    AgentRun,
    ApprovalRecord,
    ApprovalStatus,
    MessageRole,
    RunCheckpoint,
    RunMessage,
    RunMessageKind,
    RunStatistics,
    RunStatus,
)
from dotclaw.session.session import Session, SessionManager


def _build_running_run() -> AgentRun:
    """构造仓储测试所需的最小运行摘要。"""
    policy: AgentPolicySnapshot = AgentPolicySnapshot(
        agent_id="agent-1",
        identity_version="identity-v1",
        model_id="model-v1",
        max_iterations=8,
    )
    return AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        status=RunStatus.RUNNING,
        started_at="2026-07-16T00:00:00+00:00",
        policy=policy,
        input_message_id="message-user-1",
        statistics=RunStatistics(),
    )


def _build_messages() -> tuple[RunMessage, RunMessage]:
    """构造一条用户消息和一条最终助手消息。"""
    user_message: RunMessage = RunMessage(
        message_id="message-user-1",
        sequence=1,
        kind=RunMessageKind.USER_INPUT,
        role=MessageRole.USER,
        content="你好",
    )
    final_message: RunMessage = RunMessage(
        message_id="message-assistant-1",
        sequence=2,
        kind=RunMessageKind.FINAL_RESPONSE,
        role=MessageRole.ASSISTANT,
        content="你好，我可以帮助你。",
    )
    return user_message, final_message


async def test_file_run_repository_preserves_message_event_order(tmp_path: Path) -> None:
    """事件只能引用已原子保存的消息，且序号必须连续。"""
    repository: FileRunRepository = FileRunRepository(tmp_path)
    run: AgentRun = _build_running_run()
    user_message: RunMessage
    final_message: RunMessage
    user_message, final_message = _build_messages()
    await repository.create_run(run)

    missing_message_event: RunEvent = RunEvent(
        run_id=run.run_id,
        sequence=1,
        event_type=RunEventType.RUN_STARTED,
        occurred_at="2026-07-16T00:00:01+00:00",
        message_ids=(user_message.message_id,),
    )
    with pytest.raises(ValueError, match="尚未保存"):
        await repository.append_event(run.session_id, missing_message_event)

    await repository.save_messages(run.session_id, run.run_id, (user_message, final_message))
    await repository.append_event(run.session_id, missing_message_event)
    completed_event: RunEvent = RunEvent(
        run_id=run.run_id,
        sequence=2,
        event_type=RunEventType.RUN_COMPLETED,
        occurred_at="2026-07-16T00:00:02+00:00",
        message_ids=(final_message.message_id,),
    )
    await repository.append_event(run.session_id, completed_event)

    loaded_messages: tuple[RunMessage, ...] = await repository.load_messages(run.session_id, run.run_id)
    assert loaded_messages == (user_message, final_message)
    events_path: Path = tmp_path / run.session_id / "agent_runs" / run.run_id / "events.jsonl"
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 2
    assert not tuple(tmp_path.rglob("*.tmp"))


async def test_success_projection_and_checkpoint_are_isolated_by_run(tmp_path: Path) -> None:
    """成功投影、检查点与审批记录分别按职责写入独立容器。"""
    run_repository: FileRunRepository = FileRunRepository(tmp_path)
    checkpoint_repository: FileCheckpointRepository = FileCheckpointRepository(tmp_path)
    approval_repository: FileApprovalRepository = FileApprovalRepository(tmp_path)
    running_run: AgentRun = _build_running_run()
    user_message: RunMessage
    final_message: RunMessage
    user_message, final_message = _build_messages()
    await run_repository.create_run(running_run)
    await run_repository.save_messages(running_run.session_id, running_run.run_id, (user_message, final_message))

    checkpoint: RunCheckpoint = RunCheckpoint(
        checkpoint_id="checkpoint-1",
        run_id=running_run.run_id,
        session_id=running_run.session_id,
        checkpoint_sequence=1,
        event_sequence=1,
        message_sequence=1,
        agent_state={"phase": "waiting_approval", "iteration": 1},
        next_action=AgentAction.EXECUTE_TOOLS,
        pending={"kind": "approval", "approval_id": "approval-1"},
        budget={"max_iterations": 8, "tokens_in": 10, "tokens_out": 2},
    )
    await checkpoint_repository.save(checkpoint)
    assert await checkpoint_repository.load(running_run.session_id, running_run.run_id) == checkpoint

    approval: ApprovalRecord = ApprovalRecord(
        approval_id="approval-1",
        run_id=running_run.run_id,
        session_id=running_run.session_id,
        status=ApprovalStatus.PENDING,
        created_at="2026-07-16T00:00:01+00:00",
    )
    await approval_repository.create(approval)
    consumed_approval: ApprovalRecord | None = await approval_repository.consume(approval.approval_id)
    assert consumed_approval is not None
    assert consumed_approval.status is ApprovalStatus.CONSUMED
    assert await approval_repository.consume(approval.approval_id) is None

    completed_run: AgentRun = replace(
        running_run,
        status=RunStatus.COMPLETED,
        ended_at="2026-07-16T00:00:02+00:00",
        final_message_id=final_message.message_id,
    )
    await run_repository.commit_success(completed_run, final_message)
    conversation = await run_repository.load_conversation(running_run.session_id)
    assert conversation[0].content == final_message.content
    assert conversation[0].role is MessageRole.ASSISTANT

    await checkpoint_repository.delete(running_run.session_id, running_run.run_id)
    assert await checkpoint_repository.load(running_run.session_id, running_run.run_id) is None


async def test_success_projection_uses_existing_session_and_is_idempotent(tmp_path: Path) -> None:
    """只有成功运行会通过既有 SessionManager 生成一条可见 Conversation。"""
    session_manager: SessionManager = SessionManager(tmp_path)
    session: Session = Session(id="session-1")
    await session_manager.save(session)
    projector: SessionConversationProjector = SessionConversationProjector(session_manager)
    repository: FileRunRepository = FileRunRepository(tmp_path, projector)
    running_run: AgentRun = _build_running_run()
    user_message: RunMessage
    final_message: RunMessage
    user_message, final_message = _build_messages()

    failed_run: AgentRun = replace(running_run, run_id="run-failed", status=RunStatus.FAILED)
    await repository.create_run(failed_run)
    await repository.save_messages(failed_run.session_id, failed_run.run_id, (user_message, final_message))
    await repository.save_run(failed_run)
    loaded_before_success: Session | None = await session_manager.load(running_run.session_id)
    assert loaded_before_success is not None
    assert loaded_before_success.conversations == []

    await repository.create_run(running_run)
    await repository.save_messages(running_run.session_id, running_run.run_id, (user_message, final_message))
    completed_run: AgentRun = replace(
        running_run,
        status=RunStatus.COMPLETED,
        ended_at="2026-07-16T00:00:02+00:00",
        final_message_id=final_message.message_id,
    )
    await repository.commit_success(completed_run, final_message)
    await repository.commit_success(completed_run, final_message)

    loaded_session: Session | None = await session_manager.load(running_run.session_id)
    assert loaded_session is not None
    assert len(loaded_session.conversations) == 1
    assert loaded_session.conversations[0].user_query == user_message.content
    assert loaded_session.conversations[0].final_answer == final_message.content
    assert loaded_session.conversations[0].agent_run_ids == [running_run.run_id]
    assert not (tmp_path / running_run.session_id / "conversation.json").exists()


async def test_checkpoint_rejects_full_prompt_and_tool_result_payloads(tmp_path: Path) -> None:
    """恢复检查点只能保存最小控制数据，不能夹带完整上下文或工具执行结果。"""
    repository: FileCheckpointRepository = FileCheckpointRepository(tmp_path)
    checkpoint: RunCheckpoint = RunCheckpoint(
        checkpoint_id="checkpoint-guard-1",
        run_id="run-guard-1",
        session_id="session-guard-1",
        checkpoint_sequence=1,
        event_sequence=1,
        message_sequence=1,
        agent_state={"phase": "waiting_tools"},
        next_action=AgentAction.EXECUTE_TOOLS,
        pending={"call_id": "call-1"},
        budget={"max_iterations": 8},
    )

    with pytest.raises(ValueError, match="prompt"):
        await repository.save(replace(checkpoint, agent_state={"prompt": "完整系统提示词"}))
    with pytest.raises(ValueError, match="tool_result"):
        await repository.save(replace(checkpoint, pending={"tool_result": {"content": "完整工具输出"}}))

    await repository.save(checkpoint)
    checkpoint_path: Path = tmp_path / checkpoint.session_id / "agent_runs" / checkpoint.run_id / "checkpoint.json"
    assert checkpoint_path.is_file()


async def test_migration_converts_legacy_agent_run_sample(tmp_path: Path) -> None:
    """旧 AgentRun 样例可迁移为消息、事件、检查点和 Conversation 容器。"""
    project_root: Path = Path(__file__).resolve().parents[2]
    legacy_run_path: Path = project_root / "data" / "sessions" / "1a8d087e" / "agent_runs" / "5a6a8ae0.json"
    report = await migrate_agent_run(legacy_run_path, tmp_path, "1a8d087e")

    run_repository: FileRunRepository = FileRunRepository(tmp_path)
    checkpoint_repository: FileCheckpointRepository = FileCheckpointRepository(tmp_path)
    migrated_run: AgentRun | None = await run_repository.load_run("1a8d087e", "5a6a8ae0")
    migrated_messages: tuple[RunMessage, ...] = await run_repository.load_messages("1a8d087e", "5a6a8ae0")
    checkpoint: RunCheckpoint | None = await checkpoint_repository.load("1a8d087e", "5a6a8ae0")
    conversation = await run_repository.load_conversation("1a8d087e")

    assert report.message_count == 5
    assert report.event_count == 2
    assert report.checkpoint_created
    assert migrated_run is not None
    assert migrated_run.status is RunStatus.COMPLETED
    assert migrated_messages[-1].kind is RunMessageKind.FINAL_RESPONSE
    assert checkpoint is not None
    assert "messages" not in checkpoint.agent_state
    assert "tool_results" not in checkpoint.agent_state
    assert conversation[0].content == migrated_messages[-1].content


async def test_migration_reports_missing_legacy_run_with_actionable_error(tmp_path: Path) -> None:
    """迁移命令遇到缺失旧文件时必须给出可行动错误信息。"""
    missing_source: Path = tmp_path / "missing-agent-run.json"

    with pytest.raises(FileNotFoundError, match="找不到旧 AgentRun 文件"):
        await migrate_agent_run(missing_source, tmp_path, "session-1")
