"""PR6 上下文实验的无副作用统计函数。"""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt

from .eval_baseline_models import BenchmarkSample, LatencyStats
from .eval_baseline_stats import percentile, success_rate


def latency_stats(values: Sequence[float]) -> LatencyStats:
    """汇总至少一条恢复或压缩耗时。"""
    if not values:
        raise ValueError("没有可聚合的时延样本")
    return LatencyStats(len(values), percentile(values, 50), percentile(values, 95), percentile(values, 99), max(values))


def absolute_error_count(samples: Sequence[BenchmarkSample], field_name: str) -> int:
    """汇总指定整型错误观察；缺失字段拒绝伪报零错误。"""
    values = [getattr(sample, field_name) for sample in samples]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError(f"{field_name} 缺失或类型错误，不能聚合")
    return sum(values)


def budget_pass_rate(samples: Sequence[BenchmarkSample]) -> float:
    """计算已观察到预算结论的通过率。"""
    values = [sample.budget_passed for sample in samples]
    if not values or any(value is None for value in values):
        raise ValueError("budget_passed 缺失，不能聚合")
    return success_rate(sum(value is True for value in values), len(values))


def wilson_interval(errors: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """返回二项错误率的 Wilson 区间，空样本拒绝统计。"""
    if total <= 0 or errors < 0 or errors > total:
        raise ValueError("Wilson 区间输入无效")
    proportion = errors / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return center - margin, center + margin
