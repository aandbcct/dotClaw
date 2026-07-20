"""E5 成功提交恢复、候选投影与故障注入验收测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from dotclaw.runtime.adapters import CheckpointRepositoryAdapter, RunRepositoryAdapter, SessionConversationProjector
from dotclaw.runtime.application.dto import ConversationMessage
from dotclaw.runtime.application.ports import SuccessCommitFaultPort
from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextSlotSnapshot,
    ContextSlotStatus,
    StagedHistoryCompression,
    StagedHistoryCompressionStatus,
    SuccessCommitFaultPoint,
    SuccessCommitIntent,
    new_context_version,
)
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.events import RunEvent, RunEventType
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
from dotclaw.session.session import Session, SessionManager


class SuccessCommitInterrupted(RuntimeError):
    """测试用进程中断异常。"""


class FaultInjector(SuccessCommitFaultPort):
    """在指定成功提交边界模拟一次进程中断。"""

    def __init__(self, point: SuccessCommitFaultPoint) -> None:
        """设置本轮需要中断的唯一边界。"""
        self._point: SuccessCommitFaultPoint = point
        self.enabled: bool = True
        self.calls: list[SuccessCommitFaultPoint] = []

    async def inject(self, point: SuccessCommitFaultPoint) -> None:
        """记录边界，并在启用时抛出可恢复中断。"""
        self.calls.append(point)
        if self.enabled and point is self._point:
            raise SuccessCommitInterrupted(point.value)


async def _prepare_pending_success(
    root: Path,
    fault_injector: FaultInjector,
) -> tuple[RunRepositoryAdapter, CheckpointRepositoryAdapter, SessionManager, AgentRun, RunMessage, RunEvent, SuccessCommitIntent]:
    """创建已保存消息、checkpoint 与待成功提交意图的最小 Run。"""
    session_manager: SessionManager = SessionManager(root)
    session: Session = await session_manager.create(agent_id="agent-e5")
    repository: RunRepositoryAdapter = RunRepositoryAdapter(
        root,
        SessionConversationProjector(session_manager),
        fault_injector,
    )
    checkpoint_repository: CheckpointRepositoryAdapter = CheckpointRepositoryAdapter(root)
    policy: AgentPolicySnapshot = AgentPolicySnapshot("agent-e5", "policy-e5", "model-e5", 4)
    running: AgentRun = AgentRun("run-e5", session.id, "agent-e5", RunStatus.RUNNING, "", policy, "input-e5")
    user_message: RunMessage = RunMessage("input-e5", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "测试问题")
    final_message: RunMessage = RunMessage("final-e5", 2, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "测试回答")
    completed: AgentRun = replace(running, status=RunStatus.COMPLETED, ended_at="2026-07-20T00:00:00+00:00", final_message_id=final_message.message_id)
    completed_event: RunEvent = RunEvent(
        completed.run_id,
        1,
        RunEventType.RUN_COMPLETED,
        "2026-07-20T00:00:00+00:00",
        (final_message.message_id,),
    )
    intent: SuccessCommitIntent = SuccessCommitIntent(
        conversation_id="conversation-run-e5",
        latest_candidate_id=None,
        target_status=RunStatus.COMPLETED,
        run_id=completed.run_id,
        session_id=completed.session_id,
    )
    checkpoint: RunCheckpoint = RunCheckpoint(
        "checkpoint-e5",
        completed.run_id,
        completed.session_id,
        1,
        0,
        2,
        {"phase": "finalizing", "iteration": 1},
        AgentAction.INVOKE_LLM,
        {},
        {},
    )
    await repository.create_run(running)
    await repository.save_messages(session.id, running.run_id, (user_message, final_message))
    await checkpoint_repository.save(checkpoint)
    return repository, checkpoint_repository, session_manager, completed, final_message, completed_event, intent


async def test_success_commit_recovers_every_persistence_boundary_idempotently(tmp_path: Path) -> None:
    """投影、事件和 Run 文件任一前后中断后均可恢复为一次完整成功事实。"""
    point: SuccessCommitFaultPoint
    for point in SuccessCommitFaultPoint:
        root: Path = tmp_path / point.value
        injector: FaultInjector = FaultInjector(point)
        repository, checkpoint_repository, session_manager, completed, final_message, completed_event, intent = await _prepare_pending_success(root, injector)

        try:
            await repository.commit_success(completed, final_message, completed_event, intent)
        except SuccessCommitInterrupted:
            pass
        else:
            raise AssertionError("故障注入未中断成功提交")

        assert await checkpoint_repository.load(completed.session_id, completed.run_id) is not None
        injector.enabled = False
        await repository.recover_pending_success_commits()
        await repository.recover_pending_success_commits()
        recovered: AgentRun | None = await repository.load_run(completed.session_id, completed.run_id)
        session: Session | None = await session_manager.load(completed.session_id)
        events_path: Path = root / completed.session_id / "agent_runs" / completed.run_id / "events.jsonl"
        events: list[JSONMap] = [
            require_json_map(json.loads(line))
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]

        assert recovered is not None
        assert recovered.status is RunStatus.COMPLETED
        assert recovered.success_commit_intent is None
        assert session is not None
        assert len(session.conversations) == 1
        assert events == [completed_event.to_dict()]
        assert await checkpoint_repository.load(completed.session_id, completed.run_id) is None


async def test_success_commit_projects_only_latest_history_candidate(tmp_path: Path) -> None:
    """成功路径只提交最新 staged 候选的摘要，旧 superseded 候选保持审计但不写 Session。"""
    injector: FaultInjector = FaultInjector(SuccessCommitFaultPoint.BEFORE_SESSION_PROJECTION)
    injector.enabled = False
    repository, _, session_manager, completed, final_message, completed_event, _ = await _prepare_pending_success(tmp_path, injector)
    session: Session | None = await session_manager.load(completed.session_id)
    assert session is not None
    history_conversation = session.add_conversation("旧问题", "旧回答", ["old-run"])
    await session_manager.save(session)
    older: StagedHistoryCompression = StagedHistoryCompression(
        "candidate-old",
        StagedHistoryCompressionStatus.SUPERSEDED,
        1,
        history_conversation.conversation_id,
        "old-source",
        "old-summary",
        1,
    )
    latest: StagedHistoryCompression = StagedHistoryCompression(
        "candidate-latest",
        StagedHistoryCompressionStatus.STAGED,
        1,
        history_conversation.conversation_id,
        "latest-source",
        "latest-summary",
        1,
    )
    history_slot: ContextSlotSnapshot = ContextSlotSnapshot(
        "history",
        ContextOwner.SESSION,
        ContextContributionKind.HISTORY,
        status=ContextSlotStatus.INCLUDED,
        injection_order=0,
        attributes={
            "conversation": {
                "compressed_history": {
                    "compression_version": 1,
                    "covered_through_conversation_id": history_conversation.conversation_id,
                    "content": "最新压缩摘要",
                    "content_hash": "latest-summary",
                },
            },
        },
    )
    await repository.append_context_version(
        completed.session_id,
        completed.run_id,
        new_context_version(1, (history_slot,), "context-hash", "tool-hash"),
    )
    await repository.save_staged_history_compressions(completed.session_id, completed.run_id, (older, latest))
    completed = replace(completed, staged_history_compressions=(older, latest))
    intent: SuccessCommitIntent = SuccessCommitIntent(
        "conversation-run-e5",
        latest.candidate_id,
        RunStatus.COMPLETED,
        completed.run_id,
        completed.session_id,
    )

    await repository.commit_success(completed, final_message, completed_event, intent)
    projected: Session | None = await session_manager.load(completed.session_id)
    recovered: AgentRun | None = await repository.load_run(completed.session_id, completed.run_id)

    assert projected is not None
    assert len(projected.conversations) == 2
    assert [(item.content, item.source_conversation_hash) for item in projected.history_compressions] == [
        ("最新压缩摘要", "latest-source"),
    ]
    assert recovered is not None
    assert [item.status for item in recovered.staged_history_compressions] == [
        StagedHistoryCompressionStatus.SUPERSEDED,
        StagedHistoryCompressionStatus.COMMITTED,
    ]
