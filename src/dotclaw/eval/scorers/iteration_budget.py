"""ITERATION_BUDGET Scorer：校验 LLM 调用数 / 工具调用数 / 循环次数不超过上限。"""

from __future__ import annotations

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from ._helpers import llm_spans
from .kinds import ExpectationKind


class IterationBudgetScorer:
    """比对运行统计 / Trace Span 计数与期望的迭代上限。"""

    KIND = ExpectationKind.ITERATION_BUDGET

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对迭代预算。"""
        target = (expectation.target or "llm_calls").lower()
        expected = expectation.expected
        if not isinstance(expected, int) or isinstance(expected, bool):
            return AssertionResult(expectation, False, "ITERATION_BUDGET 期望上限必须是整数")
        stats = trace.run.statistics
        if target == "llm_calls":
            actual = stats.llm_call_count
        elif target == "tool_calls":
            actual = stats.tool_call_count
        elif target == "loops":
            actual = len(llm_spans(trace))
        else:
            return AssertionResult(expectation, False, f"ITERATION_BUDGET 不支持的 target={target}")
        passed = actual <= expected
        return AssertionResult(
            expectation, passed, f"期望 {target} <= {expected}，实际 {actual}"
        )
