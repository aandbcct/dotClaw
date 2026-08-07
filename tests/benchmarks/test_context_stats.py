"""PR6 Context 统计测试。"""

import pytest

from benchmarks.context_stats import absolute_error_count, budget_pass_rate
from .helpers import make_sample


def test_error_count_rejects_missing_observation() -> None:
    """缺失污染/漂移事实时不能聚合成零错误。"""
    with pytest.raises(ValueError):
        absolute_error_count([make_sample()], "context_drift_count")


def test_budget_pass_rate_uses_observed_values() -> None:
    """预算通过率只由明确布尔观察计算。"""
    assert budget_pass_rate([make_sample(budget_passed=True), make_sample(budget_passed=False)]) == 0.5
