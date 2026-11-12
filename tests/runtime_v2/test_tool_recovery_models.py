"""工具恢复账本领域模型的静态契约覆盖。"""

from dataclasses import replace

import pytest

from dotclaw.common.tool_replay import ToolExecutionEvidence, ToolReplayPolicy, ToolSideEffect
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    ApprovalRecord,
    ApprovalStatus,
    RunCheckpoint,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    MessageRole,
    ToolCall,
)
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.state import AgentRunState, RunOutcome, RunStage, Running
from dotclaw.runtime.domain.tool_recovery import (
    ToolCallKind,
    ToolCallLedger,
    ToolCallRecord,
    ToolCallState,
    ToolEffectCertainty,
    ToolOutcomeSnapshot,
    ToolOutcomeStatus,
    ToolProjectionIntent,
    ToolRecoveryAction,
)
from dotclaw.runtime.application.dto import ToolRecoveryContract, ToolRecoveryDecision, ToolRecoveryPlan


def _run() -> AgentRun:
    return AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        state=AgentRunState(Running(RunStage.EXECUTING_TOOLS)),
        started_at="2026-11-12T00:00:00+00:00",
        policy=AgentPolicySnapshot("agent-1", "v1", "model", 5),
        input_message_id="input-1",
    )


def _record(**changes: object) -> ToolCallRecord:
    record = ToolCallRecord(
        run_id="run-1",
        call=ToolCall("call-1", "lookup", {"query": "x"}),
        source_message_id="assistant-1",
        ordinal=0,
        kind=ToolCallKind.ORDINARY,
        state=ToolCallState.PENDING,
        updated_at="2026-11-12T00:00:00+00:00",
    )
    return replace(record, **changes)


def test_tool_call_round_trip_preserves_recovery_contract_and_evidence() -> None:
    record = _record(
        state=ToolCallState.FAILED,
        replay_policy=ToolReplayPolicy.KEYED_IDEMPOTENT,
        side_effect=ToolSideEffect.PRESENT,
        replay_contract_version="provider-v2",
        idempotency_key="stable-key",
        retry_count=1,
        recovery_max_retries=3,
        recovery_initial_delay_s=0.5,
        recovery_backoff_multiplier=2.0,
        recovery_max_delay_s=8.0,
        execution_scope='{"workspace_root":"/workspace"}',
        recovery_retry_not_before="2026-11-12T00:00:01+00:00",
        last_execution_evidence=ToolExecutionEvidence.RETURNED,
        last_error=RunError(RunErrorCode.TOOL_FAILURE, "rejected", True),
        last_tool_error_code="NETWORK_ERROR",
        last_tool_error_type="network",
        attempt_started_at="2026-11-12T00:00:00+00:00",
        result=ToolOutcomeSnapshot(
            ToolOutcomeStatus.FAILED,
            "rejected",
            RunError(RunErrorCode.TOOL_FAILURE, "rejected", True),
            "result-1",
            "2026-11-12T00:00:02+00:00",
            ToolEffectCertainty.KNOWN,
            tool_error_code="NETWORK_ERROR",
            tool_error_type="network",
        ),
    )
    assert ToolCallRecord.from_dict(record.to_dict()) == record


def test_legacy_construction_defaults_and_approval_compatibility() -> None:
    record = ToolCallRecord("run-1", ToolCall("call-1", "lookup", {}), "assistant-1", 0, ToolCallKind.ORDINARY, ToolCallState.PENDING)
    assert record.replay_policy is ToolReplayPolicy.NO_AUTO_RETRY
    assert record.side_effect is ToolSideEffect.UNKNOWN
    assert record.recovery_max_retries == 0
    old_consumed = ApprovalRecord("approval-1", "run-1", "session-1", ApprovalStatus.CONSUMED, "2026-11-12T00:00:00+00:00")
    assert old_consumed.decision is None
    assert ApprovalRecord(**old_consumed.__dict__) == old_consumed


def test_ledger_rejects_unknown_schema_and_duplicate_call_identity() -> None:
    record = _record()
    ledger = ToolCallLedger("session-1", "run-1", 1, (record,))
    assert ToolCallLedger.from_dict(ledger.to_dict()) == ledger
    unsupported = ledger.to_dict()
    unsupported["schema_version"] = 2
    with pytest.raises(ValueError, match="版本"):
        ToolCallLedger.from_dict(unsupported)
    with pytest.raises(ValueError, match="call_id"):
        ToolCallLedger("session-1", "run-1", 1, (record, record))
    with pytest.raises(ValueError, match="run_id"):
        ToolCallLedger("session-1", "run-1", 1, (replace(record, run_id="other-run"),))


def test_record_decoder_requires_updated_at_and_result_kind_matches_call_kind() -> None:
    encoded = _record().to_dict()
    del encoded["updated_at"]
    with pytest.raises(ValueError, match="updated_at"):
        ToolCallRecord.from_dict(encoded)
    result = ToolOutcomeSnapshot(
        ToolOutcomeStatus.COMPLETED, "ok", None, "result-1", "2026-11-12T00:00:00+00:00", ToolEffectCertainty.KNOWN,
    )
    with pytest.raises(ValueError, match="消息类别"):
        _record(kind=ToolCallKind.DELEGATION, state=ToolCallState.SUCCEEDED, result=result)
    with pytest.raises(ValueError, match="消息类别"):
        _record(
            state=ToolCallState.SUCCEEDED,
            kind=ToolCallKind.ORDINARY,
            result=replace(result, message_kind=RunMessageKind.DELEGATION_RESULT),
        )


def test_record_rejects_boolean_counters_and_invalid_recovery_budget() -> None:
    with pytest.raises(ValueError, match="ordinal"):
        _record(ordinal=True)
    with pytest.raises(ValueError, match="retries"):
        _record(recovery_max_retries=True)
    with pytest.raises(ValueError, match="退避"):
        _record(recovery_backoff_multiplier=0.5)


def test_nested_pending_projection_round_trip_preserves_new_checkpoint_and_approval_fields() -> None:
    run = _run()
    message = RunMessage("assistant-1", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-1", "lookup", {}),))
    checkpoint = RunCheckpoint(
        "checkpoint-1", "run-1", "session-1", 2, 5, 3,
        {"tool_batch_id": "assistant-1", "pending_call_ids": ["call-1"]}, {},
        AgentAction.EXECUTE_TOOLS, tool_ledger_revision=2, replay_context=True,
    )
    base_checkpoint = RunCheckpoint(
        "checkpoint-base", "run-1", "session-1", 1, 4, 2, {}, {},
        AgentAction.INVOKE_LLM, tool_ledger_revision=1, replay_context=False,
    )
    approval = ApprovalRecord(
        "approval-1", "run-1", "session-1", ApprovalStatus.APPROVED, "2026-11-12T00:00:00+00:00",
        call_id="call-1", decision=True, resolved_at="2026-11-12T00:00:01+00:00",
    )
    event = RunEvent(
        "run-1", 5, RunEventType.TOOL_RECOVERY_BLOCKED, "2026-11-12T00:00:02+00:00",
        data={"call_id": "call-1", "reason": "provider_changed", "attempt": 1},
    )
    intent = ToolProjectionIntent("commit-1", 2, (message,), (event,), checkpoint, run, run, base_checkpoint, approval)
    ledger = ToolCallLedger("session-1", "run-1", 2, (_record(),), intent)
    assert ToolCallLedger.from_dict(ledger.to_dict()) == ledger
    malformed = ledger.to_dict()
    malformed_intent = malformed["pending_projection"]
    malformed_event = malformed_intent["events"][0]
    malformed_event["data"] = {"call_id": "call-1", "attempt": 1}
    with pytest.raises(ValueError, match="reason"):
        ToolCallLedger.from_dict(malformed)


def test_checkpoint_and_approval_new_fields_keep_old_defaults() -> None:
    checkpoint = RunCheckpoint("checkpoint-1", "run-1", "session-1", 1, 0, 0, {}, {}, AgentAction.INVOKE_LLM)
    assert checkpoint.tool_ledger_revision is None
    assert checkpoint.replay_context is None
    approved = ApprovalRecord(
        "approval-1", "run-1", "session-1", ApprovalStatus.APPROVED, "2026-11-12T00:00:00+00:00",
        call_id="call-1", decision=True, resolved_at="2026-11-12T00:00:01+00:00",
    )
    assert approved.to_dict()["decision"] is True
    with pytest.raises(ValueError, match="call_id"):
        ApprovalRecord("approval-2", "run-1", "session-1", ApprovalStatus.APPROVED, "2026-11-12T00:00:00+00:00", decision=True, resolved_at="2026-11-12T00:00:01+00:00")


def test_transition_graph_covers_waiting_approval_and_rejects_unlisted_edges() -> None:
    pending = _record()
    pending.validate_transition(ToolCallState.STARTED)
    pending.validate_transition(ToolCallState.WAITING_APPROVAL)
    waiting = _record(state=ToolCallState.WAITING_APPROVAL, approval_id="approval-1")
    waiting.validate_transition(ToolCallState.STARTED)
    waiting.validate_transition(ToolCallState.CANCELLED)
    with pytest.raises(ValueError, match="不允许"):
        waiting.validate_transition(ToolCallState.SUCCEEDED)
    with pytest.raises(ValueError, match="副作用"):
        _record(replay_policy=ToolReplayPolicy.SAFE_RETRY, replay_contract_version="v1")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ToolCallState.PENDING, target)
        for target in (ToolCallState.STARTED, ToolCallState.WAITING_APPROVAL, ToolCallState.WAITING_CHILD, ToolCallState.CANCELLED)
    ]
    + [
        (ToolCallState.STARTED, target)
        for target in (ToolCallState.STARTED, ToolCallState.SUCCEEDED, ToolCallState.FAILED, ToolCallState.WAITING_APPROVAL, ToolCallState.WAITING_CHILD, ToolCallState.UNCERTAIN, ToolCallState.CANCELLED)
    ]
    + [(ToolCallState.WAITING_APPROVAL, target) for target in (ToolCallState.STARTED, ToolCallState.CANCELLED)]
    + [(ToolCallState.WAITING_CHILD, target) for target in (ToolCallState.SUCCEEDED, ToolCallState.CANCELLED)]
    + [(state, state) for state in (ToolCallState.SUCCEEDED, ToolCallState.FAILED, ToolCallState.CANCELLED, ToolCallState.UNCERTAIN)],
)
def test_state_graph_accepts_declared_edges(source: ToolCallState, target: ToolCallState) -> None:
    # 此处只覆盖状态边；attempt/retry_count 由 C05/C10a 提交边界校验。
    options: dict[str, object] = {"state": source}
    if target is ToolCallState.WAITING_CHILD:
        options["kind"] = ToolCallKind.DELEGATION
    if source is ToolCallState.WAITING_APPROVAL:
        options["approval_id"] = "approval-1"
    if source is ToolCallState.WAITING_CHILD:
        options.update(kind=ToolCallKind.DELEGATION, child_run_id="child-1")
    if source in {ToolCallState.SUCCEEDED, ToolCallState.FAILED, ToolCallState.CANCELLED}:
        status = {
            ToolCallState.SUCCEEDED: ToolOutcomeStatus.COMPLETED,
            ToolCallState.FAILED: ToolOutcomeStatus.FAILED,
            ToolCallState.CANCELLED: ToolOutcomeStatus.CANCELLED,
        }[source]
        options["result"] = ToolOutcomeSnapshot(
            status,
            "output",
            RunError(RunErrorCode.TOOL_FAILURE, "failed") if source is ToolCallState.FAILED else None,
            None if source is ToolCallState.CANCELLED else "result-1",
            "2026-11-12T00:00:00+00:00",
            ToolEffectCertainty.UNKNOWN,
        )
    if source is ToolCallState.UNCERTAIN:
        options["uncertainty_reason"] = "process_interrupted"
    record = _record(**options)
    record.validate_transition(target)


def test_recovery_contract_and_plan_use_typed_decisions_and_next_sequences() -> None:
    contract = ToolRecoveryContract(
        ToolReplayPolicy.KEYED_IDEMPOTENT,
        "provider-v1",
        "2026-11-12T01:00:00+00:00",
        ToolSideEffect.PRESENT,
        recovery_max_retries=2,
        execution_scope='{"workspace_root":"/workspace"}',
    )
    ledger = ToolCallLedger("session-1", "run-1", 1, (_record(),))
    plan = ToolRecoveryPlan(_run(), (), None, ledger, 3, 2, (ToolRecoveryDecision("call-1", ToolRecoveryAction.RETRY, "keyed"),))
    assert contract.policy is ToolReplayPolicy.KEYED_IDEMPOTENT
    assert plan.next_event_sequence == 3
    assert plan.next_message_sequence == 2
    with pytest.raises(ValueError, match="非空版本"):
        ToolRecoveryContract(ToolReplayPolicy.SAFE_RETRY)


def test_delegation_result_kind_is_valid_for_delegation_call() -> None:
    result = ToolOutcomeSnapshot(
        ToolOutcomeStatus.COMPLETED,
        "child output",
        None,
        "result-1",
        "2026-11-12T00:00:00+00:00",
        ToolEffectCertainty.KNOWN,
        message_kind=RunMessageKind.DELEGATION_RESULT,
        child_outcome=RunOutcome.COMPLETED,
    )
    delegation = _record(kind=ToolCallKind.DELEGATION, state=ToolCallState.SUCCEEDED, result=result)
    assert delegation.result is result


def test_cancelled_outcome_may_have_no_message_but_failed_outcome_must_have_error() -> None:
    ToolOutcomeSnapshot(
        ToolOutcomeStatus.CANCELLED, "", None, None, "2026-11-12T00:00:00+00:00", ToolEffectCertainty.UNKNOWN,
    )
    with pytest.raises(ValueError, match="运行错误"):
        ToolOutcomeSnapshot(
            ToolOutcomeStatus.FAILED, "bad", None, "result-1", "2026-11-12T00:00:00+00:00", ToolEffectCertainty.KNOWN,
        )
