"""TOOL_ARGUMENT Scorer：校验某次工具调用的关键参数子集。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from ._helpers import message_by_id, tool_spans
from .kinds import ExpectationKind

if TYPE_CHECKING:
    from ..trace.models import TraceSpan


class ToolArgumentScorer:
    """比对工具调用实际参数与期望的关键参数子集。"""

    KIND = ExpectationKind.TOOL_ARGUMENT

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """比对工具调用参数子集。"""
        expected = expectation.expected
        if not isinstance(expected, dict):
            return AssertionResult(expectation, False, "TOOL_ARGUMENT 期望参数为对象")
        target = expectation.target
        span: TraceSpan | None = None
        for candidate in tool_spans(trace):
            call_id = str(candidate.attributes.get("call_id", ""))
            tool_name = str(candidate.attributes.get("tool_name", ""))
            if target and (call_id == target or tool_name == target):
                span = candidate
                break
        if span is None:
            return AssertionResult(expectation, False, f"找不到匹配 target={target} 的工具调用 Span")
        source_id = span.message_ids[0] if span.message_ids else None
        message = message_by_id(trace, source_id) if source_id else None
        if message is None:
            return AssertionResult(expectation, False, f"工具调用的源响应消息 {source_id} 不在 Trace 中")
        actual_args: dict | None = None
        for call in message.tool_calls:
            if str(call.call_id) == target or str(call.name) == target:
                actual_args = call.arguments
                break
        if actual_args is None:
            return AssertionResult(expectation, False, f"源消息中找不到 target={target} 的 tool_call 参数")
        mismatched = [
            key for key, value in expected.items() if key not in actual_args or actual_args[key] != value
        ]
        passed = not mismatched
        evidence = f"期望参数子集 {expected}，实际 {actual_args}"
        if not passed:
            evidence += f"，缺失/不符：{mismatched}"
        return AssertionResult(expectation, passed, evidence)
