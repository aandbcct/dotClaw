"""ITERATION_BUDGET Scorer：LLM 调用数 / 工具调用数 / 循环数不超过上限。"""

import asyncio
from ..eval_testkit import run_case_to_trace, tool_case
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.iteration_budget import IterationBudgetScorer

_SCORER = IterationBudgetScorer()


async def _trace():
    return await run_case_to_trace(tool_case())


async def test_llm_calls_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("iteration_budget", "llm_calls", 2))
    assert result.passed is True


async def test_llm_calls_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("iteration_budget", "llm_calls", 1))
    assert result.passed is False


async def test_tool_calls_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("iteration_budget", "tool_calls", 1))
    assert result.passed is True


async def test_tool_calls_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("iteration_budget", "tool_calls", 0))
    assert result.passed is False


async def test_loops_pass():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("iteration_budget", "loops", 2))
    assert result.passed is True


async def test_loops_fail():
    trace = await _trace()
    result = _SCORER.score(trace, Expectation("iteration_budget", "loops", 1))
    assert result.passed is False
