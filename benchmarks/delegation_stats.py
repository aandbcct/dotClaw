"""PR7 委派统计纯函数。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .eval_baseline_models import BenchmarkSample
from .eval_baseline_stats import percentile


@dataclass(frozen=True)
class DelegationSummary:
    """委派场景聚合结果（正式非 warmup 样本）。"""
    sample_count: int
    passed_count: int
    error_count: int
    wilson_lower: float
    wilson_upper: float
    suspend_to_backfill_p50_ms: float | None
    suspend_to_backfill_p95_ms: float | None
    parent_end_to_end_p50_ms: float | None
    parent_end_to_end_p95_ms: float | None


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """计算二项成功率 Wilson 95% 区间；零样本没有可报告区间。"""
    if total <= 0:
        return (0.0, 0.0)
    rate = successes / total
    denominator = 1.0 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * total)) / total) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def summarize(samples: Sequence[BenchmarkSample]) -> DelegationSummary:
    """仅聚合正式样本，warmup 不进入成功率与时延分位数。"""
    formal = [sample for sample in samples if not sample.is_warmup]
    successes = sum(sample.passed for sample in formal)
    backfill = [sample.suspend_to_backfill_ms for sample in formal if sample.suspend_to_backfill_ms is not None]
    end_to_end = [sample.parent_end_to_end_ms for sample in formal if sample.parent_end_to_end_ms is not None]
    lower, upper = wilson_interval(successes, len(formal))
    return DelegationSummary(len(formal), successes, len(formal) - successes, lower, upper,
                             percentile(backfill, 50.0) if backfill else None,
                             percentile(backfill, 95.0) if backfill else None,
                             percentile(end_to_end, 50.0) if end_to_end else None,
                             percentile(end_to_end, 95.0) if end_to_end else None)
