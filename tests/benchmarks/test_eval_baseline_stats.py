"""PR1 Benchmark 统计纯函数：分位数、成功率、Case / 全局聚合与快照构建。"""

from __future__ import annotations

import pytest

from benchmarks.eval_baseline_stats import (
    aggregate_case_summary,
    aggregate_global_summary,
    build_snapshot,
    percentile,
    success_rate,
)
from benchmarks.eval_baseline_models import ExecutionSource

from .helpers import make_failing_sample, make_sample


# --------------------------------------------------------------------------- #
# 分位数与成功率
# --------------------------------------------------------------------------- #


def test_percentile_empty_raises() -> None:
    """空序列无法计算分位数，必须明确失败。"""
    with pytest.raises(ValueError):
        percentile([], 50.0)


def test_percentile_single_value() -> None:
    """单元素序列返回该元素本身。"""
    assert percentile([7.0], 50.0) == 7.0


def test_percentile_out_of_range_raises() -> None:
    """分位数超出 [0, 100] 必须失败。"""
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], 101.0)


def test_percentile_linear_interpolation() -> None:
    """线性插值口径：与 numpy 默认一致。"""
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 50.0) == pytest.approx(2.5)
    assert percentile(values, 0.0) == pytest.approx(1.0)
    assert percentile(values, 100.0) == pytest.approx(4.0)
    assert percentile(values, 95.0) == pytest.approx(3.85)


def test_percentile_unsorted_input() -> None:
    """输入无需排序，输出与排序后一致。"""
    assert percentile([4.0, 1.0, 3.0, 2.0], 50.0) == pytest.approx(2.5)


def test_success_rate_basic() -> None:
    """成功率同时保留分子 / 分母语义。"""
    assert success_rate(3, 10) == pytest.approx(0.3)
    assert success_rate(0, 10) == 0.0
    assert success_rate(10, 10) == 1.0


def test_success_rate_invalid_raises() -> None:
    """总样本数为 0 或通过数越界必须失败。"""
    with pytest.raises(ValueError):
        success_rate(0, 0)
    with pytest.raises(ValueError):
        success_rate(11, 10)


# --------------------------------------------------------------------------- #
# Case 聚合
# --------------------------------------------------------------------------- #


def test_aggregate_case_summary_counts() -> None:
    """Case 汇总正确聚合样本数、通过 / 失败数与成功率。"""
    samples = [
        make_sample(case_id="tool_success", attempt=0, wall_duration_ms=10.0),
        make_sample(case_id="tool_success", attempt=1, wall_duration_ms=20.0),
        make_failing_sample(case_id="tool_success", attempt=2, wall_duration_ms=30.0),
    ]
    summary = aggregate_case_summary(samples)
    assert summary.case_id == "tool_success"
    assert summary.sample_count == 3
    assert summary.passed_count == 2
    assert summary.failed_count == 1
    assert summary.success_rate == pytest.approx(2 / 3)
    assert summary.failure_kinds == {"assertion": 1}


def test_aggregate_case_summary_percentiles() -> None:
    """时延分布与 Trace 关键路径分位数按同一口径计算。"""
    samples = [
        make_sample(case_id="c", attempt=i, wall_duration_ms=float(10 + i))
        for i in range(4)
    ]
    summary = aggregate_case_summary(samples)
    assert summary.wall_duration_ms.sample_count == 4
    assert summary.wall_duration_ms.p50_ms == pytest.approx(11.5)
    assert summary.wall_duration_ms.max_ms == 13.0
    # trace_metrics 的 critical_path_ms 与 llm_duration_ms 均被聚合
    assert summary.trace_metrics_ms["critical_path_ms"].p50_ms == pytest.approx(2.0)
    assert summary.trace_metrics_ms["llm_duration_ms"].p50_ms == pytest.approx(2.0)


def test_aggregate_case_summary_call_counts() -> None:
    """调用数与 Trace 健康指标正确加总；缺失 token 事实不参与。"""
    samples = [
        make_sample(case_id="c", attempt=0),
        make_sample(case_id="c", attempt=1),
    ]
    summary = aggregate_case_summary(samples)
    assert summary.llm_call_count_total == 4
    assert summary.tool_call_count_total == 2
    assert summary.trace_available_count == 2
    assert summary.trace_missing_count == 0
    assert summary.failed_tool_count_total == 0


def test_aggregate_case_summary_empty_raises() -> None:
    """空样本集无法聚合，必须明确失败。"""
    with pytest.raises(ValueError):
        aggregate_case_summary([])


def test_aggregate_case_summary_mixed_cases_raises() -> None:
    """同一 Case 汇总收到多个 case_id 必须失败。"""
    samples = [
        make_sample(case_id="a"),
        make_sample(case_id="b"),
    ]
    with pytest.raises(ValueError):
        aggregate_case_summary(samples)


def test_aggregate_case_summary_trace_missing() -> None:
    """Trace 缺失的样本计入 trace_missing_count，不当作成功。"""
    samples = [
        make_sample(case_id="c", attempt=0, trace_available=True),
        make_sample(case_id="c", attempt=1, trace_available=False),
    ]
    summary = aggregate_case_summary(samples)
    assert summary.trace_available_count == 1
    assert summary.trace_missing_count == 1


# --------------------------------------------------------------------------- #
# 全局聚合
# --------------------------------------------------------------------------- #


def test_aggregate_global_summary_across_cases() -> None:
    """全局汇总跨 Case 同口径聚合失败归因与调用数。"""
    samples = [
        make_sample(case_id="tool_success", attempt=0, wall_duration_ms=10.0),
        make_sample(case_id="tool_success", attempt=1, wall_duration_ms=20.0),
        make_failing_sample(case_id="approval_rejected", attempt=0, wall_duration_ms=15.0),
        make_failing_sample(case_id="approval_rejected", attempt=1, wall_duration_ms=25.0),
    ]
    summary = aggregate_global_summary(samples)
    assert summary.sample_count == 4
    assert summary.passed_count == 2
    assert summary.failed_count == 2
    assert summary.success_rate == pytest.approx(0.5)
    assert summary.failure_kinds == {"assertion": 2}
    assert summary.llm_call_count_total == 8
    assert summary.tool_call_count_total == 4
    assert summary.wall_duration_ms.p50_ms == pytest.approx(17.5)
    assert summary.wall_duration_ms.max_ms == 25.0


def test_aggregate_global_summary_empty_raises() -> None:
    """空样本集无法聚合全局汇总。"""
    with pytest.raises(ValueError):
        aggregate_global_summary([])


# --------------------------------------------------------------------------- #
# 快照构建
# --------------------------------------------------------------------------- #


def _build(samples, **overrides) -> object:
    """构造快照的公共参数。"""
    kwargs = dict(
        snapshot_id="20260806T091530Z_b6426cc",
        generated_at="2026-08-06T09:15:30+00:00",
        git_commit="b6426cc",
        dataset="runtime_core_v1",
        environment={
            "python_version": "3.13.5",
            "platform": "Windows",
            "config_hash": "cfg-hash-1",
            "eval_schema_version": "1.0",
        },
        warmup=5,
        repeat=2,
        samples=samples,
        samples_path="samples/20260806T091530Z_b6426cc.jsonl",
        samples_content_summary={"line_count": 10, "byte_count": 2048},
    )
    kwargs.update(overrides)
    return build_snapshot(**kwargs)


def test_build_snapshot_excludes_warmup() -> None:
    """warmup 样本不进入快照正式统计。"""
    samples = [
        make_sample(case_id="tool_success", attempt=0, is_warmup=True, wall_duration_ms=999.0),
        make_sample(case_id="tool_success", attempt=1, wall_duration_ms=10.0),
        make_sample(case_id="tool_success", attempt=2, wall_duration_ms=20.0),
    ]
    snapshot = _build(samples)
    assert snapshot.warmup == 5
    assert snapshot.repeat == 2
    assert snapshot.global_summary.sample_count == 2
    assert snapshot.global_summary.wall_duration_ms.max_ms == 20.0
    assert len(snapshot.cases) == 1
    assert snapshot.cases[0].sample_count == 2
    assert snapshot.cases[0].wall_duration_ms.max_ms == 20.0


def test_build_snapshot_no_formal_samples_raises() -> None:
    """JSONL 中缺少正式采样时快照生成必须失败。"""
    samples = [make_sample(attempt=0, is_warmup=True)]
    with pytest.raises(ValueError):
        _build(samples)


def test_build_snapshot_preserves_case_order() -> None:
    """快照 Case 顺序与样本首次出现顺序一致（即 Dataset 稳定顺序）。"""
    samples = [
        make_sample(case_id="approval_rejected", attempt=0),
        make_sample(case_id="approval_resume", attempt=0),
        make_sample(case_id="context_retention", attempt=0),
        make_sample(case_id="tool_success", attempt=0),
    ]
    snapshot = _build(samples)
    assert [case.case_id for case in snapshot.cases] == [
        "approval_rejected",
        "approval_resume",
        "context_retention",
        "tool_success",
    ]


def test_build_snapshot_all_failed_still_generates() -> None:
    """全部断言失败但 Trace 完整时，仍生成快照并正确统计失败归因。"""
    samples = [
        make_failing_sample(case_id="tool_success", attempt=0, wall_duration_ms=5.0),
        make_failing_sample(case_id="tool_success", attempt=1, wall_duration_ms=6.0),
    ]
    snapshot = _build(samples)
    assert snapshot.global_summary.passed_count == 0
    assert snapshot.global_summary.failed_count == 2
    assert snapshot.global_summary.success_rate == 0.0
    assert snapshot.global_summary.failure_kinds == {"assertion": 2}
    assert snapshot.cases[0].failure_kinds == {"assertion": 2}


# --------------------------------------------------------------------------- #
# 历史来源聚合：null 缺失值不参与相应指标
# --------------------------------------------------------------------------- #


def _historical_sample(case_id: str = "tool_success", *, attempt: int = 0, wall_duration_ms: float = 3.0) -> object:
    """构造历史适配器来源样本：无 Trace、token/内部时延缺失为 None。"""
    return make_sample(
        case_id=case_id,
        attempt=attempt,
        wall_duration_ms=wall_duration_ms,
        execution_source="historical_adapter",
        source_commit="4e4cdd3a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
        scenario_id="tool_success",
        evidence_kind="final_result",
        trace_available=False,
        run_id=None,
        trace_source=None,
        trace_metrics={
            "llm_duration_ms": None,
            "tool_duration_ms": None,
            "approval_wait_ms": None,
            "critical_path_ms": None,
            "failed_tool_count": 0,
        },
        run_statistics={
            "duration_ms": None,
            "llm_call_count": 1,
            "tool_call_count": 1,
            "tokens_in": None,
            "tokens_out": None,
        },
    )


def test_historical_sample_aggregation_null_excluded() -> None:
    """历史缺失的 Trace/内部时延/token 以 null 保留，不参与分位数与调用统计。"""
    samples = [
        _historical_sample(attempt=0, wall_duration_ms=3.0),
        _historical_sample(attempt=1, wall_duration_ms=5.0),
    ]
    summary = aggregate_case_summary(samples)
    # 外层 wall_duration_ms 仍可聚合（历史也有外围端到端耗时）
    assert summary.wall_duration_ms.sample_count == 2
    assert summary.wall_duration_ms.p50_ms == pytest.approx(4.0)
    # 内部时延缺失 → 不产生 trace_metrics_ms 分位数
    assert summary.trace_metrics_ms == {}
    # 调用计数来自历史最低语义事实，正常加总
    assert summary.llm_call_count_total == 2
    assert summary.tool_call_count_total == 2
    # Trace 缺失被列入健康指标
    assert summary.trace_available_count == 0
    assert summary.trace_missing_count == 2


def test_historical_build_snapshot_propagates_source() -> None:
    """历史样本聚合的快照保留执行来源与场景标识。"""
    samples = [_historical_sample(attempt=0), _historical_sample(attempt=1)]
    snapshot = _build(
        samples,
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        scenario_id="tool_success",
    )
    assert snapshot.execution_source.value == "historical_adapter"
    assert snapshot.scenario_id == "tool_success"
    assert snapshot.global_summary.sample_count == 2
    assert snapshot.global_summary.passed_count == 2
