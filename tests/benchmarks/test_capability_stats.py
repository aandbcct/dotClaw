"""PR5 安全链统计口径测试。"""

from __future__ import annotations

from benchmarks.capability_stats import performance_summary, summarize_security
from tests.benchmarks.helpers import make_sample


def test_security_summary_excludes_environment_skip() -> None:
    """环境跳过不进入安全通过分母。"""
    samples = [
        make_sample(decision_pass=True, handler_entered=0, actual_error_code="POLICY_DENIED", measurement_mode=None),
        make_sample(case_id="skip", decision_pass=None, capability_reason="junction unavailable", measurement_mode=None),
    ]
    summary = summarize_security(samples)
    assert (summary.applicable, summary.passed, summary.skipped, summary.denied_handler_entries) == (1, 1, 1, 0)


def test_performance_summary_excludes_warmup() -> None:
    """性能预热记录不得混入正式分位数。"""
    samples = [
        make_sample(is_warmup=True, measurement_mode="full_security_chain", pre_handler_duration_ms=99.0),
        make_sample(measurement_mode="full_security_chain", pre_handler_duration_ms=1.0),
        make_sample(attempt=1, measurement_mode="full_security_chain", pre_handler_duration_ms=3.0),
    ]
    summary = performance_summary(samples, "full_security_chain")
    assert summary["sample_count"] == 2
    assert summary["p50_ms"] == 2.0
