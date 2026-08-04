"""OUTPUT_ASSERTION Scorer：最终助手回答的精确 / 包含 / 正则评分。"""

import asyncio
from ..eval_testkit import run_case_to_trace, tool_case
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.output_assertion import OutputAssertionScorer

_SCORER = OutputAssertionScorer()

FINAL = "The weather is sunny today"


async def _trace():
    return await run_case_to_trace(tool_case())


async def test_exact_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("output_assertion", "text", FINAL, {"mode": "exact"}))
    assert result.passed is True


async def test_exact_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("output_assertion", "text", "xyz", {"mode": "exact"}))
    assert result.passed is False


async def test_contains_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("output_assertion", "text", "sunny", {"mode": "contains"}))
    assert result.passed is True


async def test_contains_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("output_assertion", "text", "xyz", {"mode": "contains"}))
    assert result.passed is False


async def test_regex_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("output_assertion", "text", "wea.*sunny", {"mode": "regex"}))
    assert result.passed is True


async def test_regex_valid_no_match_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("output_assertion", "text", "zzznomatch", {"mode": "regex"}))
    assert result.passed is False
