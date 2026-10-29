"""消息内执行步骤只投影已有审计事实，不调用外部模型。"""

from dataclasses import replace

import pytest

from dotclaw.channel.web.app import _run_activity
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun
from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome, Suspended, SuspendReason, RunStage


def _run(outcome=RunOutcome.COMPLETED):
    return AgentRun("r", "s", "default", AgentRunState(mode=Ended(outcome)), "t", AgentPolicySnapshot("default", "v1", "fake", 10), "input")


def _event(sequence, kind, call_id=None, status=None):
    data = {"call_id": call_id} if call_id else {}
    if status:
        data["status"] = status
    return RunEvent("r", sequence, RunEventType(kind), f"t{sequence}", data=data)


def test_multiround_parallel_steps_stay_ordered_and_deduplicated():
    events = (
        _event(1, "llm_started"), _event(2, "llm_completed"),
        _event(3, "tool_started", "a"), _event(4, "tool_started", "b"),
        _event(5, "tool_completed", "b", "failed"), _event(6, "tool_completed", "a", "completed"),
        _event(7, "llm_started"), _event(8, "llm_completed"),
    )
    result = _run_activity(_run(), tuple(reversed(events)) + (events[0],), (), False)
    assert result.event_sequence == 8
    assert [(step.id, step.kind, step.status, step.completed_at) for step in result.steps] == [
        ("r:1", "model", "completed", "t2"), ("r:3", "tool", "completed", "t6"),
        ("r:4", "tool", "failed", "t5"), ("r:7", "model", "completed", "t8"),
    ]


@pytest.mark.parametrize("outcome,status", [(RunOutcome.FAILED, "failed"), (RunOutcome.CANCELLED, "cancelled"), (RunOutcome.COMPLETED, "interrupted")])
def test_terminal_only_closes_unfinished_steps(outcome, status):
    events = (_event(1, "tool_started", "a"), _event(2, "tool_completed", "a", "completed"), _event(3, "llm_started"), _event(4, "tool_started", "b"))
    result = _run_activity(_run(outcome), events, (), False)
    assert [step.status for step in result.steps] == ["completed", status, status]


def test_missing_start_and_approval_keep_available_facts():
    events = (_event(2, "llm_completed"), _event(4, "tool_completed", "a", "approval_required"))
    result = _run_activity(_run(), events, (), False)
    assert [(step.id, step.started_at, step.status) for step in result.steps] == [("r:2", "", "completed"), ("r:4", "", "approval_required")]
    assert result.tools[0].call_id == "a"
    assert _run_activity(_run(), (), (), False).steps == []


@pytest.mark.parametrize("approval,call_id", [("B", "call-b"), ("missing", None)])
def test_approval_projection_only_uses_current_approval_event(approval, call_id):
    run = replace(_run(), state=AgentRunState(mode=Suspended(SuspendReason.APPROVAL, approval, RunStage.EXECUTING_TOOLS)))
    events = tuple(RunEvent("r", sequence, RunEventType.WAITING_APPROVAL, "t", data={"approval_id": key, "call_id": call}) for sequence, key, call in [(1, "A", "call-a"), (2, "B", "call-b"), (3, "A", "wrong-latest")])
    result = _run_activity(run, events, (), False)
    assert result.approval_call_id == call_id
    assert _run_activity(_run(), events, (), False).approval_call_id is None
