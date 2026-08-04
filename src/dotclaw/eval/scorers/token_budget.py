"""TOKEN_BUDGET Scorer：校验 tokens_in / tokens_out / total 不超过上限。"""

from __future__ import annotations

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from .kinds import ExpectationKind


class TokenBudgetScorer:
    """比对权威运行统计中的 token 计数与期望上限。"""

    KIND = ExpectationKind.TOKEN_BUDGET

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对 token 预算。"""
        target = (expectation.target or "tokens_in").lower()
        expected = expectation.expected
        if not isinstance(expected, int) or isinstance(expected, bool):
            return AssertionResult(expectation, False, "TOKEN_BUDGET 期望上限必须是整数")
        stats = trace.run.statistics
        if target == "tokens_in":
            actual = stats.tokens_in
        elif target == "tokens_out":
            actual = stats.tokens_out
        elif target == "total":
            actual = stats.tokens_in + stats.tokens_out
        else:
            return AssertionResult(expectation, False, f"TOKEN_BUDGET 不支持的 target={target}")
        passed = actual <= expected
        return AssertionResult(
            expectation, passed, f"期望 {target} <= {expected}，实际 {actual}"
        )
