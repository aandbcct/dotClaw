"""OUTPUT_ASSERTION Scorer：对最终助手回答做精确 / 包含 / 正则断言。"""

from __future__ import annotations

import re

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from ._helpers import final_assistant_content
from .kinds import ExpectationKind


class OutputAssertionScorer:
    """比对最终助手回答与期望文本的精确 / 包含 / 正则关系。"""

    KIND = ExpectationKind.OUTPUT_ASSERTION

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对最终回答。"""
        expected = expectation.expected
        if not isinstance(expected, str):
            return AssertionResult(expectation, False, "OUTPUT_ASSERTION 期望文本必须是字符串")
        mode = str(expectation.options.get("mode", "exact")).lower()
        content = final_assistant_content(trace)
        if content is None:
            return AssertionResult(expectation, False, "Trace 中找不到最终助手回答")
        if mode == "exact":
            passed = content == expected
        elif mode == "contains":
            passed = expected in content
        elif mode == "regex":
            try:
                passed = re.search(expected, content) is not None
            except re.error:
                passed = False
        else:
            return AssertionResult(expectation, False, f"OUTPUT_ASSERTION 不支持的 mode={mode}")
        return AssertionResult(
            expectation, passed, f"mode={mode} 期望 {expected!r}，实际回答长度={len(content)}"
        )
