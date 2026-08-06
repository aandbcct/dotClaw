"""PR2 当前/历史对照：可比性检查、变化率、Wilson 区间与 Markdown 报告。"""

from __future__ import annotations

import pytest

from benchmarks.eval_baseline_models import (
    BenchmarkSnapshot,
    CaseSummary,
    ExecutionSource,
    GlobalSummary,
    LatencyStats,
)
from benchmarks.historical_compare import (
    build_comparison_report,
    check_comparability,
    percent_change,
    wilson_interval,
)

_CURRENT_ENV = {
    "python_version": "3.13.5",
    "platform": "Windows-11-10.0.26200-SP0",
    "config_hash": "b9bea591d3252a9a",
    "eval_schema_version": "1.0",
}
_HISTORICAL_ENV = {
    "python_version": "3.13.5",
    "platform": "Windows-11-10.0.26200-SP0",
    "config_hash": "historical-agent-v1-fixed-fixture",
    "eval_schema_version": "1.0",
}


def _latency(p50: float, p95: float = 2.0, p99: float = 3.0, mx: float = 4.0, count: int = 30) -> LatencyStats:
    """构造时延分布。"""
    return LatencyStats(sample_count=count, p50_ms=p50, p95_ms=p95, p99_ms=p99, max_ms=mx)


def _case(
    *,
    case_id: str = "tool_success",
    passed: int = 30,
    sample_count: int = 30,
    wall: LatencyStats | None = None,
    llm_total: int = 30,
    tool_total: int = 30,
    trace_available: int = 30,
) -> CaseSummary:
    """构造 Case 汇总。"""
    return CaseSummary(
        case_id=case_id,
        sample_count=sample_count,
        passed_count=passed,
        failed_count=sample_count - passed,
        success_rate=passed / sample_count,
        wall_duration_ms=wall or _latency(1.0),
        llm_call_count_total=llm_total,
        tool_call_count_total=tool_total,
        trace_available_count=trace_available,
        trace_missing_count=sample_count - trace_available,
    )


def _global(passed: int = 30, sample_count: int = 30, wall: LatencyStats | None = None) -> GlobalSummary:
    """构造全局汇总。"""
    return GlobalSummary(
        sample_count=sample_count,
        passed_count=passed,
        failed_count=sample_count - passed,
        success_rate=passed / sample_count,
        wall_duration_ms=wall or _latency(1.0),
        llm_call_count_total=passed,
        tool_call_count_total=passed,
        trace_available_count=passed,
        trace_missing_count=0,
    )


def _snapshot(
    *,
    execution_source: ExecutionSource,
    scenario_id: str = "tool_success",
    dataset: str = "runtime_core_v1",
    repeat: int = 30,
    warmup: int = 5,
    environment: dict | None = None,
    cases: tuple[CaseSummary, ...] | None = None,
    fixture_fingerprints: dict | None = None,
    git_commit: str = "HEAD",
    snapshot_id: str = "snap-1",
) -> BenchmarkSnapshot:
    """构造快照。"""
    case_list = cases or (_case(),)
    fingerprints: dict = (
        fixture_fingerprints
        if fixture_fingerprints is not None
        else {case.case_id: "fp-1234567890abcdef" for case in case_list}
    )
    return BenchmarkSnapshot(
        snapshot_id=snapshot_id,
        generated_at="2026-08-06T00:00:00+00:00",
        git_commit=git_commit,
        dataset=dataset,
        warmup=warmup,
        repeat=repeat,
        global_summary=_global(
            passed=sum(c.passed_count for c in case_list),
            sample_count=sum(c.sample_count for c in case_list),
            wall=case_list[0].wall_duration_ms,
        ),
        samples_path="samples/x.jsonl",
        execution_source=execution_source,
        scenario_id=scenario_id,
        fixture_fingerprints=fingerprints,
        environment=environment or _CURRENT_ENV,
        cases=case_list,
        samples_content_summary={"line_count": 1},
    )


def _current() -> BenchmarkSnapshot:
    """当前 Eval 快照：tool_success 全通过、Wall P50 0.83ms。"""
    return _snapshot(
        execution_source=ExecutionSource.CURRENT_EVAL,
        cases=(_case(wall=_latency(0.83, 1.64, 2.08, 2.19)),),
        git_commit="c4511b3",
        snapshot_id="20260806T033707Z_c4511b3",
    )


def _historical() -> BenchmarkSnapshot:
    """历史适配快照：tool_success 全通过、Wall P50 5.0ms。"""
    return _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        cases=(_case(wall=_latency(5.0, 6.0, 7.0, 8.0), trace_available=0),),
        git_commit="4e4cdd3a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
        snapshot_id="20260806T040000Z_4e4cdd3",
    )


# --------------------------------------------------------------------------- #
# 纯函数：变化率与 Wilson 区间
# --------------------------------------------------------------------------- #


def test_percent_change_basic() -> None:
    """正常计算 (current - historical) / historical。"""
    assert percent_change(6.0, 5.0) == pytest.approx(0.2)
    assert percent_change(4.0, 5.0) == pytest.approx(-0.2)
    assert percent_change(5.0, 5.0) == 0.0


def test_percent_change_zero_historical_returns_none() -> None:
    """历史值为 0 时不计算变化率。"""
    assert percent_change(1.0, 0.0) is None


def test_wilson_interval() -> None:
    """Wilson 95% 区间：全通过与部分通过的口径。"""
    lo, hi = wilson_interval(30, 30)
    assert lo > 0.85 and hi == pytest.approx(1.0)
    lo2, hi2 = wilson_interval(15, 30)
    assert lo2 < 0.5 < hi2
    assert lo2 < hi2


def test_wilson_interval_invalid_total() -> None:
    """总样本数为 0 时明确失败。"""
    with pytest.raises(ValueError):
        wilson_interval(0, 0)


# --------------------------------------------------------------------------- #
# 可比性检查
# --------------------------------------------------------------------------- #


def test_comparability_matches() -> None:
    """满足条件的当前/历史快照判定为可比。"""
    result = check_comparability(_current(), _historical())
    assert result.comparable is True
    assert result.reasons == ()
    assert result.shared_scenarios == ("tool_success",)


def test_comparability_dataset_mismatch() -> None:
    """Dataset 不一致：不可比。"""
    historical = _historical()
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        dataset="other_dataset",
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("Dataset" in reason for reason in result.reasons)


def test_comparability_repeat_mismatch() -> None:
    """正式 repeat 不一致：不可比。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        repeat=10,
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("repeat" in reason for reason in result.reasons)


def test_comparability_platform_mismatch() -> None:
    """机器标识不一致：不可比。"""
    env = dict(_HISTORICAL_ENV)
    env["platform"] = "Linux-5.15"
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=env,
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("机器标识" in reason for reason in result.reasons)


def test_comparability_python_mismatch() -> None:
    """Python 主/次版本不一致：不可比。"""
    env = dict(_HISTORICAL_ENV)
    env["python_version"] = "3.12.0"
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=env,
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("Python" in reason for reason in result.reasons)


def test_comparability_unknown_config_rejected() -> None:
    """任一配置哈希缺失时不可比。"""
    env = dict(_HISTORICAL_ENV)
    env["config_hash"] = "unknown"
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=env,
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("配置" in reason for reason in result.reasons)


def test_comparability_no_shared_scenario() -> None:
    """无共享场景：不可比。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        scenario_id="approval_resume",
        cases=(_case(case_id="approval_resume"),),
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("无共享场景" in reason for reason in result.reasons)


def test_comparability_tampered_scenario_rejected() -> None:
    """快照场景标识与 Case 列表不一致（疑似篡改）：拒绝计算百分比。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        scenario_id="tool_success,approval_resume",  # 声明两个但只有一个 case
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("篡改" in reason for reason in result.reasons)


def test_comparability_fixture_fingerprint_mismatch_rejected() -> None:
    """共享场景固定夹具指纹不一致：拒绝计算百分比（严格可比性门槛）。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        fixture_fingerprints={"tool_success": "fp-9999different"},
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("固定夹具指纹不一致" in reason for reason in result.reasons)


def test_comparability_fixture_fingerprint_missing_rejected() -> None:
    """任一侧缺少共享场景的固定夹具指纹：拒绝计算百分比。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        fixture_fingerprints={},
        git_commit="4e4cdd3",
    )
    result = check_comparability(_current(), historical)
    assert result.comparable is False
    assert any("缺少共享场景" in reason for reason in result.reasons)


# --------------------------------------------------------------------------- #
# 对照报告
# --------------------------------------------------------------------------- #


def test_report_contains_shared_comparison() -> None:
    """可比时报告包含成功率、时延分位数、调用数与变化率。"""
    report = build_comparison_report(_current(), _historical(), shared_scenarios=("tool_success",))
    assert "可比" in report
    assert "30/30" in report  # 成功率分子/分母
    assert "-83.4%" in report or "-83.40%" in report  # (0.83-5)/5
    assert "LLM 调用均值" in report
    assert "Tool 调用均值" in report
    assert "Wilson" in report


def test_report_incomparable_lists_reasons() -> None:
    """不可比时报告列出原因且不计算变化率。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        repeat=10,
        git_commit="4e4cdd3",
    )
    report = build_comparison_report(_current(), historical, shared_scenarios=("tool_success",))
    assert "不可比" in report
    assert "repeat" in report
    assert "共享场景对照" not in report


def test_report_historical_zero_baseline_notes_only() -> None:
    """历史值为 0 的指标仅列原值并注明不计算变化率。"""
    historical = _snapshot(
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        environment=_HISTORICAL_ENV,
        cases=(_case(wall=_latency(0.0, 0.0, 0.0, 0.0), trace_available=0),),
        git_commit="4e4cdd3",
    )
    report = build_comparison_report(_current(), historical, shared_scenarios=("tool_success",))
    assert "历史值为 0，不计算变化率" in report


def test_report_historical_missing_trace_noted() -> None:
    """历史缺失 Trace 的说明写入不可比指标章节。"""
    report = build_comparison_report(_current(), _historical(), shared_scenarios=("tool_success",))
    assert "null" in report
    assert "不参与变化率" in report


# --------------------------------------------------------------------------- #
# compare CLI
# --------------------------------------------------------------------------- #


def test_compare_cli_writes_report(tmp_path: Path) -> None:
    """compare 子命令读取两份快照 JSON 并写出对照 Markdown。"""
    import json

    from benchmarks.historical_baseline import main

    current_path = tmp_path / "current.json"
    historical_path = tmp_path / "historical.json"
    report_path = tmp_path / "comparison.md"
    current_path.write_text(json.dumps(_current().to_dict(), ensure_ascii=False), encoding="utf-8")
    historical_path.write_text(json.dumps(_historical().to_dict(), ensure_ascii=False), encoding="utf-8")

    code = main([
        "compare",
        "--current", str(current_path),
        "--historical", str(historical_path),
        "--output", str(report_path),
    ])
    assert code == 0
    text = report_path.read_text(encoding="utf-8")
    assert "当前/历史" in text
    assert "可比" in text


def test_compare_cli_incomparable_returns_nonzero(tmp_path: Path) -> None:
    """不可比时 compare 仍写出报告但返回非零退出码。"""
    import json

    from benchmarks.historical_baseline import main

    current_path = tmp_path / "current.json"
    historical_path = tmp_path / "historical.json"
    report_path = tmp_path / "comparison.md"
    current_path.write_text(json.dumps(_current().to_dict(), ensure_ascii=False), encoding="utf-8")
    # 修改 repeat 使不可比
    historical = _historical()
    from benchmarks.eval_baseline_models import BenchmarkSnapshot
    import dataclasses

    historical = dataclasses.replace(historical, repeat=10)
    historical_path.write_text(json.dumps(historical.to_dict(), ensure_ascii=False), encoding="utf-8")

    code = main([
        "compare",
        "--current", str(current_path),
        "--historical", str(historical_path),
        "--output", str(report_path),
    ])
    assert code == 1
    text = report_path.read_text(encoding="utf-8")
    assert "不可比" in text
    assert "共享场景对照" not in text
