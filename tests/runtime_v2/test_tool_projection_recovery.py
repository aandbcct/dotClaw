"""工具恢复投影的静态回归样本；本阶段不执行这些样本。"""

from dataclasses import replace
import json
import uuid

import pytest

from dotclaw.eval.environment import InMemoryCheckpointRepository
from dotclaw.eval.fixtures import FixtureApprovalRepository
from dotclaw.eval.models import FixtureMatchMode
from dotclaw.runtime.adapters.in_memory_run_repository import InMemoryRunRepository
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, MessageRole, RunCheckpoint, RunMessage, RunMessageKind, ToolCall
from dotclaw.runtime.domain.tool_recovery import ToolEffectCertainty, ToolOutcomeSnapshot, ToolOutcomeStatus
from dotclaw.runtime.application.dto import ToolRecoveryContract
from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running
from dotclaw.runtime.domain.tool_recovery import ToolCallKind, ToolCallLedger, ToolCallRecord, ToolCallState


class _FailingRunRepository(InMemoryRunRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failure_stage: str | None = None

    def _fail(self, stage: str) -> None:
        if self.failure_stage == stage:
            self.failure_stage = None
            raise OSError(f"injected {stage} projection interruption")

    async def save_tool_ledger(self, ledger, *, expected_revision: int) -> None:
        stage = "ledger_intent" if ledger.pending_projection is not None else "ledger_clear"
        self._fail(stage)
        await super().save_tool_ledger(ledger, expected_revision=expected_revision)
        self._fail(f"{stage}_after")

    async def ensure_tool_messages(self, session_id: str, run_id: str, messages) -> None:
        self._fail("messages")
        await super().ensure_tool_messages(session_id, run_id, messages)
        self._fail("messages_after")

    async def ensure_tool_events(self, session_id: str, events) -> None:
        self._fail("events")
        await super().ensure_tool_events(session_id, events)
        self._fail("events_after")

    async def ensure_tool_run(self, base_run, target_run) -> None:
        self._fail("run")
        await super().ensure_tool_run(base_run, target_run)
        self._fail("run_after")


class _FailingCheckpointRepository(InMemoryCheckpointRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failure_stage: str | None = None

    async def ensure_tool_checkpoint(self, base, target) -> None:
        if self.failure_stage == "checkpoint":
            self.failure_stage = None
            raise OSError("injected checkpoint projection interruption")
        await super().ensure_tool_checkpoint(base, target)
        if self.failure_stage == "checkpoint_after":
            self.failure_stage = None
            raise OSError("injected checkpoint_after projection interruption")


class _FailingApprovalRepository(FixtureApprovalRepository):
    def __init__(self) -> None:
        super().__init__((), FixtureMatchMode.STRICT)
        self.fail_next_ensure = True

    async def ensure(self, record) -> None:
        if self.fail_next_ensure:
            self.fail_next_ensure = False
            raise OSError("injected approval projection interruption")
        await super().ensure(record)


def _run() -> AgentRun:
    return AgentRun(
        "run-1", "session-1", "agent-1", AgentRunState(Running(RunStage.EXECUTING_TOOLS)),
        "2026-11-12T00:00:00+00:00", AgentPolicySnapshot("agent-1", "v1", "model", 5), "input-1",
    )


def _checkpoint() -> RunCheckpoint:
    return RunCheckpoint(
        "checkpoint-1", "run-1", "session-1", 1, 0, 0,
        {"tool_batch_id": "assistant-1", "pending_call_ids": ["call-1"]}, {}, AgentAction.EXECUTE_TOOLS,
    )


async def _seed() -> tuple[_FailingRunRepository, _FailingCheckpointRepository, ApprovalService, ToolRecoveryService, AgentRun, RunCheckpoint]:
    runs = _FailingRunRepository()
    checkpoints = _FailingCheckpointRepository()
    approvals = ApprovalService(FixtureApprovalRepository((), FixtureMatchMode.STRICT))
    service = ToolRecoveryService(runs, checkpoints, approvals)
    run = _run()
    checkpoint = _checkpoint()
    record = ToolCallRecord(
        "run-1", ToolCall("call-1", "lookup", {"query": "x"}), "assistant-1", 0,
        ToolCallKind.ORDINARY, ToolCallState.PENDING,
    )
    await runs.create_run(run)
    await checkpoints.save(checkpoint)
    await runs.save_tool_ledger(ToolCallLedger("session-1", "run-1", 1, (record,)), expected_revision=0)
    return runs, checkpoints, approvals, service, run, checkpoint


@pytest.mark.parametrize("failure_stage", (
    "ledger_intent", "ledger_intent_after", "messages", "messages_after", "events", "events_after",
    "checkpoint", "checkpoint_after", "run", "run_after", "ledger_clear", "ledger_clear_after",
))
async def test_projection_intent_recovers_after_each_persisted_stage(failure_stage: str) -> None:
    runs, checkpoints, approvals, service, run, checkpoint = await _seed()
    runs.failure_stage = failure_stage if not failure_stage.startswith("checkpoint") else None
    checkpoints.failure_stage = failure_stage if failure_stage.startswith("checkpoint") else None

    with pytest.raises(OSError, match=failure_stage):
        await service.begin_attempt(run, "call-1", checkpoint)

    if failure_stage == "ledger_intent":
        await service.begin_attempt(run, "call-1", checkpoint)
    else:
        resumed = ToolRecoveryService(runs, checkpoints, approvals)
        await resumed.recover_projection("session-1", "run-1")
        await resumed.recover_projection("session-1", "run-1")

    committed = await runs.load_tool_ledger("session-1", "run-1")
    events = await runs.load_events("session-1", "run-1")
    assert committed is not None and committed.pending_projection is None
    assert committed.calls[0].attempt == 1 and committed.calls[0].retry_count == 0
    assert len(events) == 1 and events[0].event_type is RunEventType.TOOL_STARTED
    assert events[0].data["tool_name"] == "lookup"
    assert events[0].data["source_response_message_id"] == "assistant-1"
    assert events[0].message_ids == ("assistant-1",)
    assert events[0].data["tool_event_semantics"] == 2
    assert events[0].data["tool_protocol_version"] == 1
    assert events[0].data["attempt"] == 1
    expected_commit_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["run-1", 2], ensure_ascii=False, separators=(",", ":"))).hex
    assert events[0].data["commit_id"] == expected_commit_id
    persisted_run = await runs.load_run("session-1", "run-1")
    assert persisted_run is not None and persisted_run.statistics.tool_call_count == 0
    assert await checkpoints.load("session-1", "run-1") == replace(
        checkpoint,
        checkpoint_id="run-1-checkpoint-2",
        checkpoint_sequence=2,
        event_sequence=1,
        tool_ledger_revision=2,
    )


async def test_approval_projection_is_replayed_from_saved_intent() -> None:
    runs = _FailingRunRepository()
    checkpoints = _FailingCheckpointRepository()
    approval_repository = _FailingApprovalRepository()
    approvals = ApprovalService(approval_repository)
    service = ToolRecoveryService(runs, checkpoints, approvals)
    run = _run()
    checkpoint = _checkpoint()
    record = ToolCallRecord(
        "run-1", ToolCall("call-1", "lookup", {"query": "x"}), "assistant-1", 0,
        ToolCallKind.ORDINARY, ToolCallState.PENDING,
    )
    await runs.create_run(run)
    await checkpoints.save(checkpoint)
    await runs.save_tool_ledger(ToolCallLedger("session-1", "run-1", 1, (record,)), expected_revision=0)

    with pytest.raises(OSError, match="approval projection"):
        await service.wait_approval(run, "call-1", "approval-1", checkpoint)

    resumed = ToolRecoveryService(runs, checkpoints, approvals)
    await resumed.recover_projection("session-1", "run-1")
    await resumed.recover_projection("session-1", "run-1")
    committed = await runs.load_tool_ledger("session-1", "run-1")
    approval = await approvals.find("approval-1")
    events = await runs.load_events("session-1", "run-1")
    assert committed is not None and committed.pending_projection is None
    assert approval is not None and approval.call_id == "call-1"
    assert len(events) == 1 and events[0].event_type is RunEventType.WAITING_APPROVAL
    assert events[0].message_ids == ("assistant-1",)
    assert events[0].data["source_response_message_id"] == "assistant-1"
    assert events[0].data["tool_name"] == "lookup"
    assert events[0].data["tool_event_semantics"] == 2
    assert events[0].data["tool_protocol_version"] == 1


async def test_result_commit_adds_logical_call_stat_once_and_keeps_result_event_metadata() -> None:
    runs, checkpoints, approvals, service, run, checkpoint = await _seed()
    started = await service.begin_attempt(run, "call-1", checkpoint)
    checkpoint_after_start = await checkpoints.load("session-1", "run-1")
    assert checkpoint_after_start is not None
    outcome = ToolOutcomeSnapshot(
        ToolOutcomeStatus.COMPLETED, "ok", None, "result-1", "2026-11-12T00:00:01+00:00",
        ToolEffectCertainty.KNOWN,
    )
    await service.commit_outcome(run, "call-1", outcome, checkpoint_after_start)
    await service.commit_outcome(run, "call-1", outcome, checkpoint_after_start)

    persisted_run = await runs.load_run("session-1", "run-1")
    events = await runs.load_events("session-1", "run-1")
    assert started.attempt == 1
    assert persisted_run is not None and persisted_run.statistics.tool_call_count == 1
    assert events[-1].event_type is RunEventType.TOOL_COMPLETED
    assert events[-1].message_ids == ("result-1",)
    assert events[-1].data["result_message_id"] == "result-1"
    assert events[-1].data["tool_name"] == "lookup"
    assert events[-1].data["source_response_message_id"] == "assistant-1"
    assert events[-1].data["attempt"] == 1
    assert events[-1].data["tool_event_semantics"] == 2
    assert events[-1].data["tool_protocol_version"] == 1
    expected_commit_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["run-1", 4], ensure_ascii=False, separators=(",", ":"))).hex
    assert events[-1].data["commit_id"] == expected_commit_id


async def test_plan_batch_does_not_count_unexecuted_logical_calls() -> None:
    runs = InMemoryRunRepository()
    checkpoints = InMemoryCheckpointRepository()
    approvals = ApprovalService(FixtureApprovalRepository((), FixtureMatchMode.STRICT))
    service = ToolRecoveryService(runs, checkpoints, approvals)
    run = _run()
    checkpoint = _checkpoint()
    source = RunMessage(
        "assistant-1", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
        tool_calls=(ToolCall("call-1", "lookup", {"query": "x"}),),
    )
    await runs.create_run(run)
    await runs.save_messages("session-1", "run-1", (source,))
    await checkpoints.save(checkpoint)

    ledger = await service.plan_batch(run, source, {"call-1": ToolRecoveryContract()}, checkpoint)

    persisted_run = await runs.load_run("session-1", "run-1")
    assert ledger.calls[0].state is ToolCallState.PENDING
    assert persisted_run is not None and persisted_run.statistics.tool_call_count == 0


async def test_cancelled_call_without_result_does_not_increment_logical_call_stat() -> None:
    runs, checkpoints, _, service, run, checkpoint = await _seed()
    await service.begin_attempt(run, "call-1", checkpoint)
    checkpoint_after_start = await checkpoints.load("session-1", "run-1")
    assert checkpoint_after_start is not None
    outcome = ToolOutcomeSnapshot(
        ToolOutcomeStatus.CANCELLED, "", None, None, "2026-11-12T00:00:01+00:00",
        ToolEffectCertainty.UNKNOWN,
    )
    await service.commit_outcome(run, "call-1", outcome, checkpoint_after_start)

    persisted_run = await runs.load_run("session-1", "run-1")
    messages = await runs.load_messages("session-1", "run-1")
    events = await runs.load_events("session-1", "run-1")
    assert persisted_run is not None and persisted_run.statistics.tool_call_count == 0
    assert messages == ()
    assert events[-1].data["result_message_id"] == ""
    assert events[-1].message_ids == ()
