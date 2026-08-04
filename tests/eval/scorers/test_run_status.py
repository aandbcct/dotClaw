"""RUN_STATUS Scorer：运行终态（outcome / 挂起）评分。"""

import asyncio
from ..eval_testkit import approval_required_case, run_case_to_trace, tool_case
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.run_status import RunStatusScorer

_SCORER = RunStatusScorer()


async def _completed_trace():
    return await run_case_to_trace(tool_case())


async def _suspended_trace():
    return await run_case_to_trace(approval_required_case())


async def test_completed_outcome_pass():
    trace = await _completed_trace()
    result = _SCORER.score(trace, Expectation("run_status", "outcome", "completed"))
    assert result.passed is True


async def test_completed_outcome_fail():
    trace = await _completed_trace()
    result = _SCORER.score(trace, Expectation("run_status", "outcome", "failed"))
    assert result.passed is False


async def test_suspended_pass():
    trace = await _suspended_trace()
    result = _SCORER.score(trace, Expectation("run_status", "outcome", "suspended"))
    assert result.passed is True


async def test_suspended_expect_completed_fail():
    trace = await _suspended_trace()
    result = _SCORER.score(trace, Expectation("run_status", "outcome", "completed"))
    assert result.passed is False
