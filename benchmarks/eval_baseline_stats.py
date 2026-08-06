"""PR1 Eval 基线统计纯函数：分位数、成功率与按 Case / 全局聚合。

本模块只做无副作用的计算，输入为 ``BenchmarkSample`` 或已聚合的数值序列，输出
``CaseSummary`` / ``GlobalSummary`` / ``BenchmarkSnapshot``。调用方（
``eval_baseline``）负责先过滤 warmup 样本：预热记录只保留为冷启动诊断证据，
不得进入任何正式统计。

统计口径约定（对应开发计划 §6）：

- ``wall_duration_ms`` 是跨提交性能比较的端到端口径；Trace 关键路径用于解释
  Runtime 内部耗时构成，两者分开报告且不得互相替代；
- P50 / P95 / P99 只在同机、同 Python、同 Dataset、同配置、同 repeat 下才能
  用于后续提交的趋势比较；
- 成功率分子 / 分母与失败归因计数一起报告，失败或缺失数据不得静默当作成功或 0。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Mapping, Sequence

from .eval_baseline_models import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkSample,
    BenchmarkSnapshot,
    CaseSummary,
    ExecutionSource,
    GlobalSummary,
    LatencyStats,
)

# Trace 时延类指标键：与 ``TraceMetrics`` 序列化键一致，用于按 Case 汇总耗时构成。
_TRACE_DURATION_KEYS: tuple[str, ...] = (
    "llm_duration_ms",
    "tool_duration_ms",
    "approval_wait_ms",
    "critical_path_ms",
)


def percentile(values: Sequence[float], p: float) -> float:
    """线性插值分位数（0 <= p <= 100）。

    空序列抛 ``ValueError``；单元素序列返回该元素。与 numpy 默认的线性插值
    口径一致，保证后续版本对照时不因插值差异产生口径漂移。
    """
    if not values:
        raise ValueError("空序列无法计算分位数")
    if p < 0.0 or p > 100.0:
        raise ValueError(f"分位数必须位于 [0, 100]，实际 {p!r}")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def success_rate(passed: int, total: int) -> float:
    """计算成功率；总样本数必须大于 0。"""
    if total <= 0:
        raise ValueError(f"总样本数必须大于 0，实际 {total}")
    if passed < 0 or passed > total:
        raise ValueError(f"通过数 {passed} 超出样本总数 {total}")
    return passed / total


def _latency_stats(values: Sequence[float]) -> LatencyStats:
    """从一组数值构造时延分布汇总（空序列明确失败）。"""
    return LatencyStats(
        sample_count=len(values),
        p50_ms=percentile(values, 50.0),
        p95_ms=percentile(values, 95.0),
        p99_ms=percentile(values, 99.0),
        max_ms=max(values),
    )


def _int_field(sample: BenchmarkSample, mapping: Mapping[str, object], key: str) -> int:
    """读取统计映射中的整数字段；缺失或 None 视为没有该项事实，按 0 计。"""
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _trace_duration_stats(samples: Sequence[BenchmarkSample]) -> Mapping[str, LatencyStats]:
    """按 Trace 时延键聚合各键的耗时分布（仅统计出现该键的样本）。"""
    result: dict[str, LatencyStats] = {}
    for key in _TRACE_DURATION_KEYS:
        values: list[float] = []
        for sample in samples:
            value = sample.trace_metrics.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            values.append(float(value))
        if values:
            result[key] = _latency_stats(values)
    return result


def aggregate_case_summary(samples: Sequence[BenchmarkSample]) -> CaseSummary:
    """聚合单个 Case 的正式采样。

    传入样本必须全部属于同一个 ``case_id`` 且已过滤 warmup，否则明确失败。
    """
    if not samples:
        raise ValueError("Case 汇总需要至少一个样本")
    case_ids = {sample.case_id for sample in samples}
    if len(case_ids) != 1:
        raise ValueError(f"同一 Case 汇总收到多个 case_id：{sorted(case_ids)}")

    total = len(samples)
    passed = sum(1 for sample in samples if sample.passed)
    failed = total - passed
    failure_kinds = Counter(
        sample.failure_kind for sample in samples if sample.failure_kind is not None
    )
    trace_available = sum(1 for sample in samples if sample.trace_available)

    return CaseSummary(
        case_id=samples[0].case_id,
        sample_count=total,
        passed_count=passed,
        failed_count=failed,
        success_rate=success_rate(passed, total),
        failure_kinds=dict(failure_kinds),
        wall_duration_ms=_latency_stats([sample.wall_duration_ms for sample in samples]),
        trace_metrics_ms=_trace_duration_stats(samples),
        llm_call_count_total=sum(
            _int_field(sample, sample.run_statistics, "llm_call_count") for sample in samples
        ),
        tool_call_count_total=sum(
            _int_field(sample, sample.run_statistics, "tool_call_count") for sample in samples
        ),
        trace_available_count=trace_available,
        trace_missing_count=total - trace_available,
        failed_tool_count_total=sum(
            _int_field(sample, sample.trace_metrics, "failed_tool_count") for sample in samples
        ),
    )


def aggregate_global_summary(samples: Sequence[BenchmarkSample]) -> GlobalSummary:
    """跨全部 Case 的同口径聚合。

    以全部正式样本为输入，失败归因、Trace 健康与调用数为各 Case 之和，
    ``wall_duration_ms`` 直接由全体样本计算，保证与各 Case 分位数同口径。
    """
    if not samples:
        raise ValueError("全局汇总需要至少一个样本")

    total = len(samples)
    passed = sum(1 for sample in samples if sample.passed)
    failed = total - passed
    failure_kinds = Counter(
        sample.failure_kind for sample in samples if sample.failure_kind is not None
    )
    trace_available = sum(1 for sample in samples if sample.trace_available)

    return GlobalSummary(
        sample_count=total,
        passed_count=passed,
        failed_count=failed,
        success_rate=success_rate(passed, total),
        failure_kinds=dict(failure_kinds),
        wall_duration_ms=_latency_stats([sample.wall_duration_ms for sample in samples]),
        llm_call_count_total=sum(
            _int_field(sample, sample.run_statistics, "llm_call_count") for sample in samples
        ),
        tool_call_count_total=sum(
            _int_field(sample, sample.run_statistics, "tool_call_count") for sample in samples
        ),
        trace_available_count=trace_available,
        trace_missing_count=total - trace_available,
    )


def build_snapshot(
    *,
    snapshot_id: str,
    generated_at: str,
    git_commit: str,
    dataset: str,
    environment: Mapping[str, str],
    warmup: int,
    repeat: int,
    samples: Sequence[BenchmarkSample],
    samples_path: str,
    execution_source: ExecutionSource = ExecutionSource.CURRENT_EVAL,
    scenario_id: str = "",
    samples_content_summary: Mapping[str, object],
) -> BenchmarkSnapshot:
    """从采样记录构建当前基线快照。

    仅选择 ``is_warmup=False`` 的记录；缺少正式采样时快照生成必须失败，
    保证 JSONL 中正式采样缺失时不会产出可用基线。``execution_source`` 与
    ``scenario_id`` 记录快照覆盖的执行链路与业务场景集合，供跨来源对照。
    """
    formal = [sample for sample in samples if not sample.is_warmup]
    if not formal:
        raise ValueError("缺少正式采样（is_warmup=false），无法生成快照")

    # 按首次出现顺序保持稳定 Case 顺序，与 Dataset 加载顺序一致
    case_ids: list[str] = []
    for sample in formal:
        if sample.case_id not in case_ids:
            case_ids.append(sample.case_id)

    case_summaries = tuple(
        aggregate_case_summary([sample for sample in formal if sample.case_id == case_id])
        for case_id in case_ids
    )

    return BenchmarkSnapshot(
        snapshot_id=snapshot_id,
        generated_at=generated_at,
        git_commit=git_commit,
        dataset=dataset,
        warmup=warmup,
        repeat=repeat,
        global_summary=aggregate_global_summary(formal),
        samples_path=samples_path,
        schema_version=BENCHMARK_SCHEMA_VERSION,
        execution_source=execution_source,
        scenario_id=scenario_id,
        environment=dict(environment),
        cases=case_summaries,
        samples_content_summary=dict(samples_content_summary),
    )
