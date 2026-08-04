"""APPROVAL Scorer：校验审批的等待与决议结果。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace, TraceSpanStatus
from ._helpers import approval_spans
from .kinds import ExpectationKind

if TYPE_CHECKING:
    from ..trace.models import TraceSpan


class ApprovalScorer:
    """比对审批 Span 的等待 / 批准 / 拒绝结果与期望。"""

    KIND = ExpectationKind.APPROVAL

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对审批决议。"""
        expected = expectation.expected
        if not isinstance(expected, str):
            return AssertionResult(expectation, False, "APPROVAL 期望 approved/rejected/waiting/pending")
        expected = expected.lower()
        target = expectation.target
        span: TraceSpan | None = None
        for candidate in approval_spans(trace):
            approval_id = str(candidate.attributes.get("approval_id", ""))
            call_id = str(candidate.attributes.get("call_id", ""))
            if target and (approval_id == target or call_id == target):
                span = candidate
                break
        if span is None:
            return AssertionResult(expectation, False, f"找不到匹配 target={target} 的审批 Span")
        status = span.status
        if expected in ("waiting", "pending"):
            passed = status is TraceSpanStatus.WAITING
        elif expected == "approved":
            passed = status is TraceSpanStatus.COMPLETED and bool(span.attributes.get("approved", False))
        elif expected == "rejected":
            passed = status is TraceSpanStatus.CANCELLED
        else:
            return AssertionResult(expectation, False, f"APPROVAL 不支持的 expected={expected}")
        evidence = (
            f"期望 {expected}，实际 status={status.value} approved={span.attributes.get('approved')}"
        )
        return AssertionResult(expectation, passed, evidence)
