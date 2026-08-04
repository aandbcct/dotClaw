"""TOOL_SEQUENCE Scorer：校验工具调用的有序名称 / call_id 序列。"""

from __future__ import annotations

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from ._helpers import tool_spans
from .kinds import ExpectationKind


class ToolSequenceScorer:
    """比对工具调用 Span 的有序 tool_name / call_id 序列与期望。"""

    KIND = ExpectationKind.TOOL_SEQUENCE

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对有序工具序列。"""
        expected = expectation.expected
        target = (expectation.target or "tool_name").lower()
        if not isinstance(expected, list):
            return AssertionResult(expectation, False, "TOOL_SEQUENCE 期望有序列表")
        expected_seq = [str(item) for item in expected]
        spans = tool_spans(trace)
        if target == "call_id":
            actual = [str(span.attributes.get("call_id", "")) for span in spans]
        else:
            actual = [str(span.attributes.get("tool_name", "")) for span in spans]
        passed = actual == expected_seq
        return AssertionResult(
            expectation, passed, f"期望顺序 {expected_seq}，实际 {actual}"
        )
