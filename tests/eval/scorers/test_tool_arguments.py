"""TOOL_ARGUMENT Scorer：工具调用关键参数子集评分。"""

import asyncio
from ..eval_testkit import run_case_to_trace, tool_case
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.tool_arguments import ToolArgumentScorer

_SCORER = ToolArgumentScorer()


async def _trace():
    return await run_case_to_trace(tool_case())


async def test_argument_by_call_id_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_argument", "call-1", {"q": "weather"}))
    assert result.passed is True


async def test_argument_by_call_id_wrong_value_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_argument", "call-1", {"q": "rain"}))
    assert result.passed is False


async def test_argument_by_tool_name_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_argument", "search", {"q": "weather"}))
    assert result.passed is True


async def test_argument_by_tool_name_wrong_value_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("tool_argument", "search", {"q": "rain"}))
    assert result.passed is False
