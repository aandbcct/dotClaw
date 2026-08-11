"""PR5 安全矩阵与前置安全链开销统计纯函数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .eval_baseline_models import BenchmarkSample
from .eval_baseline_stats import percentile


@dataclass(frozen=True)
class CapabilitySummary:
    """安全矩阵正式样本的通过、跳过、屏障与泄露汇总。"""

    applicable: int
    passed: int
    failed: int
    skipped: int
    invalid_handler_entries: int
    denied_handler_entries: int
    unapproved_handler_entries: int
    sensitive_leak_count: int


def summarize_security(samples: Sequence[BenchmarkSample]) -> CapabilitySummary:
    """汇总非预热的安全矩阵记录；跳过不记作通过。"""
    formal = [sample for sample in samples if not sample.is_warmup and sample.measurement_mode is None]
    skipped = sum(1 for sample in formal if sample.capability_reason is not None)
    applicable = [sample for sample in formal if sample.capability_reason is None]
    return CapabilitySummary(
        applicable=len(applicable), passed=sum(sample.decision_pass is True for sample in applicable),
        failed=sum(sample.decision_pass is False for sample in applicable), skipped=skipped,
        invalid_handler_entries=sum(sample.handler_entered or 0 for sample in applicable if sample.actual_error_code == "INVALID_ARGUMENTS"),
        denied_handler_entries=sum(sample.handler_entered or 0 for sample in applicable if sample.actual_error_code == "POLICY_DENIED"),
        unapproved_handler_entries=sum(sample.handler_entered or 0 for sample in applicable if sample.actual_error_code == "APPROVAL_DENIED"),
        sensitive_leak_count=sum(sample.sensitive_leak_count or 0 for sample in applicable),
    )


def performance_summary(samples: Sequence[BenchmarkSample], mode: str) -> dict[str, float | int]:
    """聚合一个性能模式的正式 Handler-entry 时延。"""
    values = [sample.pre_handler_duration_ms for sample in samples if not sample.is_warmup and sample.measurement_mode == mode and sample.pre_handler_duration_ms is not None]
    if not values:
        raise ValueError(f"性能模式 {mode!r} 缺少正式样本")
    return {"sample_count": len(values), "p50_ms": percentile(values, 50.0), "p95_ms": percentile(values, 95.0), "max_ms": max(values)}
