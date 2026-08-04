"""POLICY Scorer：校验工具 / 审批的允许或拒绝结果。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace, TraceSpanStatus
from ._helpers import approval_spans, tool_spans
from .kinds import ExpectationKind

if TYPE_CHECKING:
    from ..trace.models import TraceSpan


class PolicyScorer:
    """比对工具或审批是否被策略允许 / 拒绝。"""

    KIND = ExpectationKind.POLICY

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对允许 / 拒绝结果。"""
        expected = expectation.expected
        if not isinstance(expected, str):
            return AssertionResult(expectation, False, "POLICY 期望 allowed/denied")
        expected = expected.lower()
        target = expectation.target

        for span in tool_spans(trace):
            call_id = str(span.attributes.get("call_id", ""))
            tool_name = str(span.attributes.get("tool_name", ""))
            if target and (call_id == target or tool_name == target):
                return self._verdict(expectation, expected, f"工具 {target}", span.status)

        for span in approval_spans(trace):
            approval_id = str(span.attributes.get("approval_id", ""))
            call_id = str(span.attributes.get("call_id", ""))
            if target and (approval_id == target or call_id == target):
                return self._verdict(expectation, expected, f"审批 {target}", span.status)

        return AssertionResult(expectation, False, f"找不到匹配 target={target} 的工具或审批 Span")

    @staticmethod
    def _verdict(
        expectation: Expectation, expected: str, label: str, status: TraceSpanStatus
    ) -> AssertionResult:
        """按期望的允许 / 拒绝核对 Span 状态。"""
        if expected not in ("allowed", "denied"):
            return AssertionResult(expectation, False, f"POLICY 不支持的 expected={expected}")
        if expected == "allowed":
            passed = status in (TraceSpanStatus.COMPLETED, TraceSpanStatus.WAITING)
        else:
            passed = status in (TraceSpanStatus.FAILED, TraceSpanStatus.CANCELLED)
        return AssertionResult(
            expectation, passed, f"{label} 期望 {expected}，实际 status={status.value}"
        )
