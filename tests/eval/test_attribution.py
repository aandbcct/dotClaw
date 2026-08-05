"""PR7 FailureAttributor：每类别最小正例、置信度边界、多故障排序与基础设施不可归因。"""

from __future__ import annotations

import dataclasses

import pytest

from dotclaw.eval.attribution import (
    ATTRIBUTION_SCHEMA_VERSION,
    AttributionResult,
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

from .eval_testkit import approval_trace, synthetic_trace, tool_status_trace, _ev
from dotclaw.runtime.domain.events import RunEventType


# ---------------------------------------------------------------------------
# 合成 Trace 构造器（在已有 helper 上叠加 error / outcome / statistics）
# ---------------------------------------------------------------------------


def _error_trace(error_code, message: str, **kw):
    """返回带 RunError 的合成 Trace。"""
    t = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
        **kw,
    )
    return dataclasses.replace(t, run=dataclasses.replace(
        t.run,
        error=RunError(error_code, message),
        statistics=kw.get("statistics", RunStatistics()),
    ))


def _failed_outcome_trace(outcome=RunOutcome.FAILED):
    """返回指定 outcom 终态的合成 Trace。"""
    from dotclaw.runtime.domain.state import AgentRunState, Ended
    t = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
    )
    return dataclasses.replace(t, run=dataclasses.replace(
        t.run,
        state=AgentRunState(mode=Ended(outcome)),
    ))


def _sresult(
    *,
    passed=True,
    failure_kind=None,
    failure_detail=None,
    assertions=(),
    trace=None,
) -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id="case-1",
        run_id="run-1",
        passed=passed,
        assertion_results=tuple(assertions),
        failure_kind=failure_kind,
        failure_detail=failure_detail,
        trace=trace,
    )


def _a(kind: str, target: str, expected, passed: bool, evidence: str = "") -> AssertionResult:
    return AssertionResult(
        Expectation(kind=kind, target=target, expected=expected),
        passed=passed,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 基础设施不可归因 → UNKNOWN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_kind", [
    EvaluationFailureKind.FIXTURE_CONFIGURATION,
    EvaluationFailureKind.TRACE_RECONSTRUCTION,
    EvaluationFailureKind.RUNTIME,
])
def test_infrastructure_failure_is_unknown(failure_kind: EvaluationFailureKind) -> None:
    result = _sresult(failure_kind=failure_kind, failure_detail="infra")
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, result)
    assert attr.category == AttributionCategory.UNKNOWN
    assert attr.confidence == "UNKNOWN"


# ---------------------------------------------------------------------------
# Context 构建失败
# ---------------------------------------------------------------------------


def test_context_build_failure_high() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "token budget exceeded")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.CONTEXT_BUILD_FAILURE
    assert attr.confidence == "HIGH"


def test_context_build_failure_medium() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "boom")
    r = _sresult(trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.CONTEXT_BUILD_FAILURE
    assert attr.confidence == "MEDIUM"


def test_tokenizer_unavailable_is_context_failure() -> None:
    t = _error_trace(RunErrorCode.TOKENIZER_UNAVAILABLE, "no tk")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.CONTEXT_BUILD_FAILURE
    assert attr.confidence == "HIGH"


# ---------------------------------------------------------------------------
# LLM 不可用
# ---------------------------------------------------------------------------


def test_llm_unavailable_from_run_error() -> None:
    t = _error_trace(RunErrorCode.LLM_FAILURE, "api err")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.LLM_UNAVAILABLE
    assert attr.confidence == "HIGH"


# ---------------------------------------------------------------------------
# 工具执行失败
# ---------------------------------------------------------------------------


def test_tool_execution_failed() -> None:
    t = tool_status_trace("failed")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.TOOL_EXECUTION_FAILED
    assert attr.confidence == "HIGH"
    assert attr.decisive_span_id is not None


def test_tool_cancelled_is_execution_failure() -> None:
    t = tool_status_trace("cancelled")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.TOOL_EXECUTION_FAILED
    assert attr.confidence == "HIGH"


# ---------------------------------------------------------------------------
# Policy 拒绝
# ---------------------------------------------------------------------------


def test_policy_denied_high() -> None:
    t = _error_trace(RunErrorCode.INVALID_STATE, "policy rejected")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.POLICY_DENIED
    assert attr.confidence == "HIGH"


# ---------------------------------------------------------------------------
# 审批拒绝
# ---------------------------------------------------------------------------


def test_approval_rejected() -> None:
    t = approval_trace(approved=False)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.APPROVAL_REJECTED
    assert attr.confidence == "HIGH"
    assert attr.decisive_span_id is not None


def test_approval_approved_not_flagged() -> None:
    """通过的审批不应被归因为拒绝。"""
    t = approval_trace(approved=True)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category != AttributionCategory.APPROVAL_REJECTED


# ---------------------------------------------------------------------------
# 委派失败
# ---------------------------------------------------------------------------


def test_delegation_failed() -> None:
    from dotclaw.runtime.domain.events import RunEventType
    events = [
        _ev(1, RunEventType.RUN_STARTED),
        _ev(2, RunEventType.LLM_STARTED, {"call_index": 0, "model_id": "m"}),
        _ev(3, RunEventType.LLM_COMPLETED, {}, message_ids=["m-llm"]),
        _ev(4, RunEventType.DELEGATION_REQUESTED,
            {"tool_call_id": "c3", "target_agent_id": "agent-2"}),
        _ev(5, RunEventType.DELEGATION_SUBMITTED,
            {"child_run_id": "child-1", "task_id": "task-1", "target_agent_id": "agent-2"}),
        _ev(6, RunEventType.DELEGATION_COMPLETED,
            {"child_run_id": "child-1", "outcome": "failed"}),
        _ev(7, RunEventType.RUN_COMPLETED),
    ]
    t = synthetic_trace(events, final_message_id="m-llm")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.DELEGATION_FAILED
    assert attr.confidence == "HIGH"


# ---------------------------------------------------------------------------
# 目标未完成
# ---------------------------------------------------------------------------


def test_goal_not_completed_high() -> None:
    t = _failed_outcome_trace(RunOutcome.FAILED)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.GOAL_NOT_COMPLETED
    assert attr.confidence == "HIGH"


def test_goal_not_completed_cancelled() -> None:
    t = _failed_outcome_trace(RunOutcome.CANCELLED)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.GOAL_NOT_COMPLETED


def test_goal_not_completed_abandoned() -> None:
    t = _failed_outcome_trace(RunOutcome.ABANDONED)
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.GOAL_NOT_COMPLETED


# ---------------------------------------------------------------------------
# 断言类归因
# ---------------------------------------------------------------------------


def test_wrong_tool_selected_from_assertion() -> None:
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("tool_sequence", "run", ["a"], False, "expected [a] got [b]"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.WRONG_TOOL_SELECTED
    assert attr.confidence == "HIGH"


def test_tool_argument_invalid_from_assertion() -> None:
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("tool_arguments", "c1", {}, False, "wrong args"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.TOOL_ARGUMENT_INVALID
    assert attr.confidence == "HIGH"


def test_iteration_budget_exceeded_from_assertion() -> None:
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("iteration_budget", "llm_calls", 2, False, "expected <=2 got 3"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.ITERATION_BUDGET_EXCEEDED
    assert attr.confidence == "HIGH"


def test_token_regression_from_assertion() -> None:
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(_a("token_budget", "tokens_in", 100, False, "expected <=100 got 200"),),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.TOKEN_REGRESSION
    assert attr.confidence == "HIGH"


# ---------------------------------------------------------------------------
# 无充分证据 → UNKNOWN
# ---------------------------------------------------------------------------


def test_no_evidence_returns_unknown() -> None:
    r = _sresult(passed=True)
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.UNKNOWN
    assert attr.confidence == "UNKNOWN"


# ---------------------------------------------------------------------------
# 多故障排序：首因优先 + 次要原因收集
# ---------------------------------------------------------------------------


def test_multiple_failures_picks_first_decisive() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "budget")
    t = dataclasses.replace(t, run=dataclasses.replace(
        t.run,
        state=__import__("dotclaw.runtime.domain.state", fromlist=["AgentRunState","Ended"])
        .AgentRunState(mode=__import__("dotclaw.runtime.domain.state", fromlist=["Ended"])
        .Ended(RunOutcome.FAILED)),
    ))
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    # _ctx_build_failure 在 _goal_not_completed 之前
    assert attr.category == AttributionCategory.CONTEXT_BUILD_FAILURE
    assert AttributionCategory.GOAL_NOT_COMPLETED in attr.secondary_causes


def test_multiple_assertion_failures_first_wins() -> None:
    r = _sresult(
        passed=False,
        failure_kind=EvaluationFailureKind.ASSERTION,
        assertions=(
            _a("tool_sequence", "run", ["a"], False, "wrong"),
            _a("iteration_budget", "llm_calls", 2, False, "exceeded"),
        ),
    )
    t = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    attr = FailureAttributor().attribute(t, r)
    assert attr.category == AttributionCategory.WRONG_TOOL_SELECTED
    assert AttributionCategory.ITERATION_BUDGET_EXCEEDED in attr.secondary_causes


# ---------------------------------------------------------------------------
# 字段完整性
# ---------------------------------------------------------------------------


def test_attribution_result_has_required_fields() -> None:
    t = _error_trace(RunErrorCode.CONTEXT_BUDGET, "err")
    r = _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t)
    attr = FailureAttributor().attribute(t, r)
    assert attr.schema_version == ATTRIBUTION_SCHEMA_VERSION
    assert isinstance(attr.category, str)
    assert attr.confidence in ("HIGH", "MEDIUM", "UNKNOWN")
    assert len(attr.evidence) > 0


def test_confidence_levels() -> None:
    t1 = _error_trace(RunErrorCode.CONTEXT_BUDGET, "err")
    assert FailureAttributor().attribute(
        t1, _sresult(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, trace=t1)
    ).confidence == "HIGH"

    t2 = _error_trace(RunErrorCode.CONTEXT_BUDGET, "err")
    assert FailureAttributor().attribute(
        t2, _sresult(trace=t2)
    ).confidence == "MEDIUM"

    t3 = synthetic_trace([_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)])
    assert FailureAttributor().attribute(
        t3, _sresult(passed=True)
    ).confidence == "UNKNOWN"


def test_unk_category_exists() -> None:
    assert AttributionCategory.UNKNOWN == "unknown"
    assert AttributionCategory("unknown") is AttributionCategory.UNKNOWN


def test_all_categories_are_valid_enum_values() -> None:
    """验证计划中列出的全部 16 个类别都已定义。"""
    expected = {
        "context_build_failure", "context_budget_exceeded", "context_information_lost",
        "llm_unavailable", "llm_invalid_action", "wrong_tool_selected",
        "tool_argument_invalid", "tool_execution_failed",
        "policy_denied", "unnecessary_approval",
        "approval_rejected", "delegation_failed",
        "goal_not_completed", "iteration_budget_exceeded",
        "token_regression", "unknown",
    }
    actual = set(AttributionCategory)
    assert actual == expected
