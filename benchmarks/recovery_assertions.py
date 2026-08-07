"""PR4 从持久化事实读取三层恢复判据。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotclaw.runtime.adapters import CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.session.session import SessionManager


@dataclass(frozen=True)
class RecoveryFacts:
    """恢复前后持久化事实的最小只读摘要。"""

    checkpoint_action_before: str | None
    checkpoint_action_after: str | None
    context_version_count_before: int
    context_version_count_after: int
    same_run_id: bool
    same_context_version: bool
    terminal_completed: bool
    tool_result_count: int
    state_transition_count: int
    completed_event_count: int
    conversation_projection_count: int
    checkpoint_cleaned: bool
    success_intent_cleaned: bool

    @property
    def control_recovery_pass(self) -> bool:
        """控制状态只判断同 Run、冻结上下文、终态和正确操作节点。"""
        return self.same_run_id and self.same_context_version and self.terminal_completed

    @property
    def internal_facts_pass(self) -> bool:
        """内部事实必须唯一收口且清理 checkpoint（检查点）与成功意图。"""
        return (
            self.terminal_completed
            and self.completed_event_count == 1
            and self.conversation_projection_count == 1
            and self.checkpoint_cleaned
            and self.success_intent_cleaned
        )


async def checkpoint_summary(root: Path, session_id: str, run_id: str) -> tuple[str | None, int]:
    """读取 checkpoint action（检查点动作）与冻结 ContextVersion（上下文版本）编号。"""
    checkpoint = await CheckpointRepositoryAdapter(root).load(session_id, run_id)
    if checkpoint is None:
        return None, 0
    return checkpoint.action.value, checkpoint.active_context_version or 0


async def collect_recovery_facts(root: Path, session_id: str, run_id: str, *, before_action: str | None, before_context_count: int) -> RecoveryFacts:
    """读取 Run、事件、消息、Session 投影和 checkpoint 的最终事实。"""
    repository = RunRepositoryAdapter(root)
    run = await repository.find_run(run_id)
    if run is None:
        raise ValueError("恢复事实读取不到 Run")
    versions = await repository.load_context_versions(session_id, run_id)
    events = await repository.load_events(session_id, run_id)
    messages = await repository.load_messages(session_id, run_id)
    checkpoint = await CheckpointRepositoryAdapter(root).load(session_id, run_id)
    session = await SessionManager(root).load(session_id)
    tool_result_count = sum(message.kind.value == "tool_result" for message in messages)
    transition_count = sum(event.event_type is RunEventType.STATE_TRANSITION for event in events)
    completed_count = sum(event.event_type is RunEventType.RUN_COMPLETED for event in events)
    projection_count = 0 if session is None else sum(run_id in item.agent_run_ids for item in session.conversations)
    return RecoveryFacts(
        checkpoint_action_before=before_action,
        checkpoint_action_after=None if checkpoint is None else checkpoint.action.value,
        context_version_count_before=before_context_count,
        context_version_count_after=len(versions),
        same_run_id=run.run_id == run_id,
        same_context_version=len(versions) == before_context_count,
        terminal_completed=run.state.outcome() is RunOutcome.COMPLETED,
        tool_result_count=tool_result_count,
        state_transition_count=transition_count,
        completed_event_count=completed_count,
        conversation_projection_count=projection_count,
        checkpoint_cleaned=checkpoint is None,
        success_intent_cleaned=run.success_commit_intent is None,
    )


def expected_checkpoint_action(mode: str) -> AgentAction:
    """固定场景对应的恢复节点；仅集中表达计划中的操作节点断言。"""
    return AgentAction.EXECUTE_TOOLS if mode.startswith("tool_") or mode == "approval_cold_rebuild" else AgentAction.INVOKE_LLM
