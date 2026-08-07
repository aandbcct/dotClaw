"""PR4 恢复实验的分层统计纯函数。

外部副作用结论不参与控制状态或内部事实成功率，避免将可观察重复混入框架
持久化正确性的统计。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

from .eval_baseline_models import (
    BenchmarkSample,
    CapabilityStatus,
    ExternalEffectStatus,
    RecoveryFaultScenario,
)
from .eval_baseline_stats import percentile, success_rate


@dataclass(frozen=True)
class RecoveryScenarioSummary:
    """单一正式故障场景的三层恢复汇总。"""

    scenario: RecoveryFaultScenario
    fault_point: str | None
    sample_count: int
    control_passed_count: int
    internal_passed_count: int
    control_success_rate: float
    internal_success_rate: float
    recovery_p50_ms: float | None
    recovery_p95_ms: float | None
    external_effect_status_counts: Mapping[str, int]
    external_duplicate_count: int


def formal_recovery_samples(samples: Sequence[BenchmarkSample]) -> list[BenchmarkSample]:
    """筛出 PR4 正式非预热样本；能力边界审计不会进入恢复率。"""
    return [
        sample for sample in samples
        if not sample.is_warmup
        and sample.capability_status is CapabilityStatus.FORMAL
        and sample.fault_scenario is not None
    ]


def aggregate_recovery_scenario(samples: Sequence[BenchmarkSample]) -> RecoveryScenarioSummary:
    """按单一场景聚合三层结论；缺失层判据视为实验记录不完整并明确失败。"""
    if not samples:
        raise ValueError("恢复场景汇总需要至少一个样本")
    scenarios = {sample.fault_scenario for sample in samples}
    points = {sample.fault_point for sample in samples}
    if len(scenarios) != 1 or None in scenarios or len(points) != 1:
        raise ValueError("恢复场景汇总必须只包含一个已知 fault_scenario 与 fault_point")
    if any(sample.control_recovery_pass is None or sample.internal_facts_pass is None for sample in samples):
        raise ValueError("正式恢复样本缺少控制状态或内部事实判据")

    count = len(samples)
    control_passed = sum(sample.control_recovery_pass is True for sample in samples)
    internal_passed = sum(sample.internal_facts_pass is True for sample in samples)
    durations = [sample.recovery_wall_duration_ms for sample in samples if sample.recovery_wall_duration_ms is not None]
    statuses = Counter(
        sample.external_effect_status.value
        for sample in samples
        if sample.external_effect_status is not None
    )
    duplicates = sum(sample.external_duplicate_count or 0 for sample in samples)
    scenario = next(iter(scenarios))
    assert scenario is not None
    return RecoveryScenarioSummary(
        scenario=scenario,
        fault_point=next(iter(points)),
        sample_count=count,
        control_passed_count=control_passed,
        internal_passed_count=internal_passed,
        control_success_rate=success_rate(control_passed, count),
        internal_success_rate=success_rate(internal_passed, count),
        recovery_p50_ms=None if not durations else percentile(durations, 50.0),
        recovery_p95_ms=None if not durations else percentile(durations, 95.0),
        external_effect_status_counts=dict(statuses),
        external_duplicate_count=duplicates,
    )


def aggregate_recovery_scenarios(samples: Sequence[BenchmarkSample]) -> tuple[RecoveryScenarioSummary, ...]:
    """按首次出现顺序聚合正式 PR4 样本。"""
    formal = formal_recovery_samples(samples)
    ordered: list[tuple[RecoveryFaultScenario, str | None]] = []
    for sample in formal:
        assert sample.fault_scenario is not None
        key = (sample.fault_scenario, sample.fault_point)
        if key not in ordered:
            ordered.append(key)
    return tuple(
        aggregate_recovery_scenario([sample for sample in formal if (sample.fault_scenario, sample.fault_point) == key])
        for key in ordered
    )


def external_status_label(status: ExternalEffectStatus) -> str:
    """将机器可读的外部副作用状态转换为报告文案。"""
    return {
        ExternalEffectStatus.NOT_OCCURRED: "未发生",
        ExternalEffectStatus.ONCE: "一次",
        ExternalEffectStatus.DUPLICATE_OBSERVED: "观察到重复",
        ExternalEffectStatus.UNKNOWN: "结果未知",
        ExternalEffectStatus.NOT_APPLICABLE: "不适用",
    }[status]
