"""TOOL_SEQUENCE Scorer：工具调用有序名称 / call_id 序列评分。"""

import asyncio
from ..eval_testkit import run_case_to_trace, tool_case
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.tool_sequence import ToolSequenceScorer

_SCORER = ToolSequenceScorer()


async def _trace():
    return await run_case_to_trace(tool_case())


async def test_tool_name_sequence_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_sequence", "tool_name", ["search"]))
    assert result.passed is True


async def test_tool_name_sequence_wrong_order_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_sequence", "tool_name", ["search", "search"]))
    assert result.passed is False


async def test_tool_name_sequence_wrong_name_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_sequence", "tool_name", ["other"]))
    assert result.passed is False


async def test_call_id_sequence_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_sequence", "call_id", ["call-1"]))
    assert result.passed is True
