"""POLICY Scorer：工具 / 审批允许或拒绝结果评分（合成 Trace 覆盖终态 Span）。"""

from ..eval_testkit import tool_status_trace
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.policy import PolicyScorer

_SCORER = PolicyScorer()


def test_completed_allowed_pass():
    trace = tool_status_trace("completed")
    result = _SCORER.score(trace, Expectation("policy", "search", "allowed"))
    assert result.passed is True


def test_completed_expect_denied_fail():
    trace = tool_status_trace("completed")
    result = _SCORER.score(trace, Expectation("policy", "search", "denied"))
    assert result.passed is False


def test_failed_denied_pass():
    trace = tool_status_trace("failed")
    result = _SCORER.score(trace, Expectation("policy", "search", "denied"))
    assert result.passed is True


def test_failed_expect_allowed_fail():
    trace = tool_status_trace("failed")
    result = _SCORER.score(trace, Expectation("policy", "search", "allowed"))
    assert result.passed is False
