"""PR7 FailureAttributor：每类别正例、时序排序、置信度边界与多故障归因。"""

from __future__ import annotations

import dataclasses

import pytest

from dotclaw.eval.attribution import (
    ATTRIBUTION_SCHEMA_VERSION,
    FailureAttributor,
)
from dotclaw.eval.attribution_rules import AttributionCategory
from dotclaw.eval.models import Expectation
from dotclaw.eval.results import (
    AssertionResult,
    EvalResult,
    EvaluationFailureKind,
)
from dotclaw.runtime.domain.facts import (
    RunError,
    RunErrorCode,
    RunStatistics,
)
from dotclaw.runtime.domain.state import RunOutcome

from .eval_testkit import (
    approval_trace,
    synthetic_trace,
    tool_status_trace,
    _ev,
)
from dotclaw.runtime.domain.events import RunEventType


# ── 构造器 ──────────────────────────────────────────────────────────


def _sresult(**kw) -> EvalResult:
    defaults: dict = dict(
        schema_version="1.0", case_id="case-1", run_id="run-1",
        passed=True, assertion_results=(),
    )
    # 允许 assertions 简写
    if "assertions" in kw:
        kw["assertion_results"] = kw.pop("assertions")
    defaults.update(kw)
    return EvalResult(**defaults)


def _a(kind: str, target: str, expected, passed: bool, ev: str = "") -> AssertionResult:
    return AssertionResult(
        Expectation(kind=kind, target=target, expected=expected),
        passed=passed, evidence=ev,
    )


def _error_trace(code, msg: str):
    t = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
    )
    return dataclasses.replace(
        t,
        run=dataclasses.replace(t.run, error=RunError(code, msg)),
    )


def _failed_trace(outcome=RunOutcome.FAILED):
    from dotclaw.runtime.domain.state import AgentRunState, Ended
    t = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
    )
    return dataclasses.replace(
        t,
        run=dataclasses.replace(t.run, state=AgentRunState(mode=Ended(outcome))),
    )


def _compression_trace():
    t = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
    )
    return dataclasses.replace(
        t,
        run=dataclasses.replace(
            t.run,
            staged_history_compressions=(StagedHistoryCompression(
                reason="token_budget",
                removed_count=5,
            ),),
        ),
    )


# ── 基础设施 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("fk", [
    EvaluationFailureKind.FIXTURE_CONFIGURATION,
    EvaluationFailureKind.TRACE_RECONSTRUCTION,
    EvaluationFailureKind.RUNTIME,
])
def test_infrastructure_is_unknown(fk: EvaluationFailureKind) -> None:
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    r = _sresult(failure_kind=fk)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.UNKNOWN
    assert a.confidence == "UNKNOWN"


# ── Context ──────────────────────────────────────────────────────────


def test_context_budget_exceeded_high() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "too big")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.CONTEXT_BUDGET_EXCEEDED
    assert a.confidence == "HIGH"


def test_context_build_failure_tokenizer() -> None:
    t = _error_trace(RunErrorCode.TOKENIZER_UNAVAILABLE, "no tk")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.CONTEXT_BUILD_FAILURE
    assert a.confidence == "HIGH"


def test_context_information_lost_from_missing_context() -> None:
    """missing_context_version TraceIssue → CONTEXT_INFORMATION_LOST。"""
    from dotclaw.runtime.domain.events import RunEvent
    from dotclaw.trace.assembler import assemble_trace
    from dotclaw.trace.models import TraceIssue, TraceIssueKind
    from tests.trace.helpers import make_run

    run = make_run(ended=True)
    events = (
        RunEvent("r1", 1, RunEventType.RUN_STARTED, "2026-01-01T00:00:00Z"),
        RunEvent("r1", 2, RunEventType.RUN_COMPLETED, "2026-01-01T00:00:01Z"),
    )
    t = assemble_trace(run, events, (), ())
    # 手动注入一个 missing_context_version Issue
    t = dataclasses.replace(
        t,
        issues=t.issues + (
            TraceIssue(
                kind=TraceIssueKind.MISSING_CONTEXT_VERSION,
                evidence="缺少上下文版本 ctx-2",
                span_id=None,
            ),
        ),
    )
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.CONTEXT_INFORMATION_LOST
    assert a.confidence == "HIGH"


# ── LLM ──────────────────────────────────────────────────────────────


def test_llm_unavailable() -> None:
    t = _error_trace(RunErrorCode.LLM_FAILURE, "api err")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.LLM_UNAVAILABLE
    assert a.confidence == "HIGH"


def test_llm_invalid_action_from_conflicting_ref() -> None:
    """手工注入 CONFLICTING_REFERENCE Issue 在 LLM Span → LLM_INVALID_ACTION。"""
    from dotclaw.runtime.domain.events import RunEvent
    from dotclaw.trace.assembler import assemble_trace
    from dotclaw.trace.models import TraceIssue, TraceIssueKind
    from dotclaw.runtime.domain.facts import RunMessage, RunMessageKind, MessageRole
    from tests.trace.helpers import make_run

    run = make_run(ended=True)
    msgs = (
        RunMessage("m-llm", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, ""),
    )
    events = (
        RunEvent("r1", 1, RunEventType.RUN_STARTED, "2026-01-01T00:00:00Z"),
        RunEvent("r1", 2, RunEventType.LLM_STARTED, "2026-01-01T00:00:01Z",
                 data={"call_index": 1, "model_id": "m", "context_version": 1}),
        RunEvent("r1", 3, RunEventType.LLM_COMPLETED, "2026-01-01T00:00:02Z",
                 data={}, message_ids=("m-llm",)),
        RunEvent("r1", 4, RunEventType.RUN_COMPLETED, "2026-01-01T00:00:03Z"),
    )
    t = assemble_trace(run, events, msgs, ())
    # 找到 LLM Span 并注入 CONFLICTING_REFERENCE Issue
    from dotclaw.eval.scorers._helpers import llm_spans
    llm_span = llm_spans(t)[0]
    t = dataclasses.replace(
        t,
        issues=t.issues + (
            TraceIssue(
                kind=TraceIssueKind.CONFLICTING_REFERENCE,
                evidence="LLM 响应引用了冲突的 tool_call_id",
                span_id=llm_span.span_id,
            ),
        ),
    )
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.LLM_INVALID_ACTION
    assert a.decisive_span_id is not None


# ── Tool ─────────────────────────────────────────────────────────────


def test_tool_execution_failed() -> None:
    t = tool_status_trace("failed")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.TOOL_EXECUTION_FAILED
    assert a.confidence == "HIGH"
    assert a.decisive_span_id is not None


def test_tool_argument_invalid() -> None:
    """PR4 断言 kind=tool_argument（非 tool_arguments）→ TOOL_ARGUMENT_INVALID。"""
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("tool_argument", "c1", {}, False, "wrong args"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.TOOL_ARGUMENT_INVALID
    assert a.confidence == "HIGH"


def test_wrong_tool_selected() -> None:
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("tool_sequence", "run", ["a"], False, "expected [a] got [b]"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.WRONG_TOOL_SELECTED


# ── Policy / Approval ────────────────────────────────────────────────


def test_policy_denied() -> None:
    t = _error_trace(RunErrorCode.INVALID_STATE, "policy rejected")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.POLICY_DENIED
    assert a.confidence == "HIGH"


def test_approval_rejected() -> None:
    t = approval_trace(approved=False)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.APPROVAL_REJECTED
    assert a.decisive_span_id is not None


def test_unnecessary_approval() -> None:
    """审批被触发但关联工具状态为 COMPLETED → UNNECESSARY_APPROVAL。"""
    from dotclaw.runtime.domain.events import RunEvent
    from dotclaw.trace.assembler import assemble_trace
    from tests.trace.helpers import make_run
    from dotclaw.runtime.domain.facts import RunMessage, RunMessageKind, MessageRole

    run = make_run(ended=True)
    events = (
        RunEvent("r1", 1, RunEventType.RUN_STARTED, "2026-01-01T00:00:00Z"),
        RunEvent("r1", 2, RunEventType.LLM_STARTED, "2026-01-01T00:00:01Z",
                 data={"call_index": 1, "model_id": "m", "context_version": 1}),
        RunEvent("r1", 3, RunEventType.LLM_COMPLETED, "2026-01-01T00:00:02Z",
                 message_ids=("m-llm",)),
        RunEvent("r1", 4, RunEventType.TOOL_STARTED, "2026-01-01T00:00:03Z",
                 data={"call_id": "c1", "tool_name": "oktool"}),
        RunEvent("r1", 5, RunEventType.TOOL_COMPLETED, "2026-01-01T00:00:04Z",
                 data={"call_id": "c1", "status": "completed"}),
        RunEvent("r1", 6, RunEventType.WAITING_APPROVAL, "2026-01-01T00:00:05Z",
                 data={"approval_id": "a1", "call_id": "c1"}),
        RunEvent("r1", 7, RunEventType.APPROVAL_RESOLVED, "2026-01-01T00:00:06Z",
                 data={"approval_id": "a1", "approved": True}),
        RunEvent("r1", 8, RunEventType.RUN_COMPLETED, "2026-01-01T00:00:07Z"),
    )
    msgs = (
        RunMessage("m-llm", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "ok"),
    )
    t = assemble_trace(run, events, msgs, ())
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.UNNECESSARY_APPROVAL


# ── Delegation / Goal / Budget ───────────────────────────────────────


def test_delegation_failed() -> None:
    events = [
        _ev(1, RunEventType.RUN_STARTED),
        _ev(2, RunEventType.LLM_STARTED, {"call_index": 0, "model_id": "m"}),
        _ev(3, RunEventType.LLM_COMPLETED, {}, message_ids=["m-llm"]),
        _ev(4, RunEventType.DELEGATION_REQUESTED,
            {"tool_call_id": "c3", "target_agent_id": "agent-2"}),
        _ev(5, RunEventType.DELEGATION_SUBMITTED,
            {"child_run_id": "c1", "task_id": "t1", "target_agent_id": "agent-2"}),
        _ev(6, RunEventType.DELEGATION_COMPLETED, {"child_run_id": "c1", "outcome": "failed"}),
        _ev(7, RunEventType.RUN_COMPLETED),
    ]
    t = synthetic_trace(events, final_message_id="m-llm")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.DELEGATION_FAILED


def test_goal_not_completed() -> None:
    t = _failed_trace(RunOutcome.FAILED)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.GOAL_NOT_COMPLETED


def test_iteration_budget_exceeded() -> None:
    r = _sresult(
        passed=False, failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("iteration_budget", "llm_calls", 2, False, "got 3"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.ITERATION_BUDGET_EXCEEDED


def test_token_regression() -> None:
    r = _sresult(
        passed=False, failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("token_budget", "tokens_in", 100, False, "got 200"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.TOKEN_REGRESSION


# ── 时序排序：最早 Span 为主因 ──────────────────────────────────────


def test_earliest_span_wins_regardless_of_rule_order() -> None:
    """较晚的工具失败 vs 较早的审批拒绝——应以审批拒绝为主因（时序更早）。"""
    t = approval_trace(approved=False)
    # 再追加一个更晚序号的前提失败工具 Span——但在时序上审批更早
    # approval_trace 的审批在 seq=7, RUN_COMPLETED 在 seq=8
    # 工具的审批 Span 比工具 Span 更晚闭合，所以工具失败不参与竞争
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.category == AttributionCategory.APPROVAL_REJECTED


def test_early_span_selected_over_later_unspanned() -> None:
    """有时序 Span 的归因优先于无 Span 的归因（如 RunError）。"""
    events = [
        _ev(1, RunEventType.RUN_STARTED),
        _ev(2, RunEventType.LLM_STARTED, {"call_index": 0, "model_id": "m"}),
        _ev(3, RunEventType.LLM_COMPLETED, {}, message_ids=["m-llm"]),
        _ev(4, RunEventType.TOOL_STARTED, {"call_id": "c1", "tool_name": "broken",
                                            "source_response_message_id": "m-llm"}),
        _ev(5, RunEventType.TOOL_COMPLETED, {"call_id": "c1", "status": "failed"}),
        _ev(6, RunEventType.RUN_COMPLETED),
    ]
    t = synthetic_trace(events, final_message_id="m-llm")
    # 同时有工具失败（有时序Span）和 Run 未完成目标（无Span）
    t = dataclasses.replace(t, run=dataclasses.replace(
        t.run,
        state=__import__("dotclaw.runtime.domain.state", fromlist=["AgentRunState","Ended"])
        .AgentRunState(mode=__import__("dotclaw.runtime.domain.state", fromlist=["Ended"])
        .Ended(RunOutcome.FAILED)),
    ))
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    # 工具 Span 有时序，应优先于无 Span 的 GOAL_NOT_COMPLETED
    assert a.category == AttributionCategory.TOOL_EXECUTION_FAILED
    assert AttributionCategory.GOAL_NOT_COMPLETED in a.secondary_causes


# ── 置信度 ──────────────────────────────────────────────────────────


def test_confidence_levels() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "err")
    assert FailureAttributor().attribute(
        t, _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    ).confidence == "HIGH"

    assert FailureAttributor().attribute(
        t, _sresult(trace=t)
    ).confidence == "MEDIUM"

    t2 = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    assert FailureAttributor().attribute(
        t2, _sresult(passed=True)
    ).confidence == "UNKNOWN"


def test_no_evidence_returns_unknown() -> None:
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    a = FailureAttributor().attribute(t, _sresult(passed=True))
    assert a.category == AttributionCategory.UNKNOWN


# ── 枚举完整性 ──────────────────────────────────────────────────────


def test_all_16_categories_exist() -> None:
    expected = {
        "context_build_failure", "context_budget_exceeded", "context_information_lost",
        "llm_unavailable", "llm_invalid_action", "wrong_tool_selected",
        "tool_argument_invalid", "tool_execution_failed",
        "policy_denied", "unnecessary_approval",
        "approval_rejected", "delegation_failed",
        "goal_not_completed", "iteration_budget_exceeded",
        "token_regression", "unknown",
    }
    assert set(AttributionCategory) == expected


def test_attribution_result_fields() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "err")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    a = FailureAttributor().attribute(t, r)
    assert a.schema_version == ATTRIBUTION_SCHEMA_VERSION
    assert isinstance(a.category, str)
    assert a.confidence in ("HIGH", "MEDIUM", "UNKNOWN")
    assert len(a.evidence) > 0
