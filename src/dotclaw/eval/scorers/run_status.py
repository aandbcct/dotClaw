"""RUN_STATUS Scorer：校验运行终态（outcome / 挂起）。"""

from __future__ import annotations

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from .kinds import ExpectationKind


class RunStatusScorer:
    """比对运行终态与期望的 outcome 或挂起状态。"""

    KIND = ExpectationKind.RUN_STATUS

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对运行终态与期望的 outcome / 挂起状态。"""
        expected = expectation.expected
        if not isinstance(expected, str):
            return AssertionResult(expectation, False, "RUN_STATUS 期望 outcome 必须是字符串")
        expected = expected.lower()
        state = trace.run.state
        if expected == "suspended":
            passed = state.is_suspended()
            return AssertionResult(
                expectation, passed, f"期望挂起(suspended)，实际 mode={state.describe()}"
            )
        outcome = state.outcome()
        actual = outcome.value if outcome is not None else state.describe()
        passed = outcome is not None and outcome.value == expected
        return AssertionResult(
            expectation,
            passed,
            f"期望 outcome={expected}，实际={actual}",
        )
