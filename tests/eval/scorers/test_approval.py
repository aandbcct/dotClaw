"""APPROVAL Scorer：审批等待与决议结果评分（合成 Trace 覆盖已决议 Span）。"""

from ..eval_testkit import approval_trace
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.approval import ApprovalScorer

_SCORER = ApprovalScorer()


def test_approved_pass():
    trace = approval_trace(True)
    result = _SCORER.score(trace, Expectation("approval", "apr-1", "approved"))
    assert result.passed is True


def test_approved_expect_rejected_fail():
    trace = approval_trace(True)
    result = _SCORER.score(trace, Expectation("approval", "apr-1", "rejected"))
    assert result.passed is False


def test_rejected_pass():
    trace = approval_trace(False)
    result = _SCORER.score(trace, Expectation("approval", "apr-1", "rejected"))
    assert result.passed is True


def test_rejected_expect_approved_fail():
    trace = approval_trace(False)
    result = _SCORER.score(trace, Expectation("approval", "apr-1", "approved"))
    assert result.passed is False
