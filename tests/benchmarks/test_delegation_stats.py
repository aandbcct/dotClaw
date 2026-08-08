"""PR7 委派统计测试。"""

from benchmarks.delegation_stats import summarize, wilson_interval
from .helpers import make_sample


def test_warmup_is_excluded_from_summary() -> None:
    """预热不进入成功率和时延统计。"""
    summary = summarize([make_sample(is_warmup=True, passed=False), make_sample(is_warmup=False, passed=True, suspend_to_backfill_ms=10.0, parent_end_to_end_ms=20.0)])
    assert (summary.sample_count, summary.passed_count, summary.error_count) == (1, 1, 0)
    assert summary.suspend_to_backfill_p50_ms == 10.0


def test_wilson_interval_is_bounded() -> None:
    """Wilson 区间必须落在概率范围内。"""
    lower, upper = wilson_interval(5, 5)
    assert 0.0 <= lower <= upper <= 1.0
