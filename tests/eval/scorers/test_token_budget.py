"""TOKEN_BUDGET Scorer：tokens_in / tokens_out / total 不超过上限（合成统计）。"""

from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.token_budget import TokenBudgetScorer
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import RunStatistics
from ..eval_testkit import _ev, synthetic_trace

_SCORER = TokenBudgetScorer()


def _trace():
    return synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
        statistics=RunStatistics(tokens_in=50, tokens_out=20),
    )


def test_tokens_in_pass():
    result = _SCORER.score(_trace(), Expectation("token_budget", "tokens_in", 100))
    assert result.passed is True


def test_tokens_in_fail():
    result = _SCORER.score(_trace(), Expectation("token_budget", "tokens_in", 10))
    assert result.passed is False


def test_tokens_out_pass():
    result = _SCORER.score(_trace(), Expectation("token_budget", "tokens_out", 20))
    assert result.passed is True


def test_tokens_out_fail():
    result = _SCORER.score(_trace(), Expectation("token_budget", "tokens_out", 10))
    assert result.passed is False


def test_total_pass():
    result = _SCORER.score(_trace(), Expectation("token_budget", "total", 70))
    assert result.passed is True


def test_total_fail():
    result = _SCORER.score(_trace(), Expectation("token_budget", "total", 50))
    assert result.passed is False
