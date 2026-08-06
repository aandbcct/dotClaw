"""PR1 Benchmark 数据模型：严格序列化 / 反序列化与往返。"""

from __future__ import annotations

import json

import pytest

from benchmarks.eval_baseline_models import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkSample,
    BenchmarkSchemaError,
    BenchmarkSnapshot,
    CaseSummary,
    GlobalSummary,
    LatencyStats,
)

from .helpers import make_failing_sample, make_sample


# --------------------------------------------------------------------------- #
# BenchmarkSample：往返与严格校验
# --------------------------------------------------------------------------- #


def test_sample_to_dict_round_trip() -> None:
    """采样记录序列化后可按同一结构原样读回。"""
    sample = make_sample()
    restored = BenchmarkSample.from_dict(sample.to_dict())
    assert restored == sample


def test_sample_json_round_trip() -> None:
    """采样记录写出 JSON 后可重新读取。"""
    sample = make_sample()
    payload = json.loads(json.dumps(sample.to_dict(), ensure_ascii=False))
    assert BenchmarkSample.from_dict(payload) == sample


def test_sample_missing_token_is_null_not_zero() -> None:
    """Fixture 未产生的 token / 时延事实保持 null，不得猜测为 0。"""
    sample = make_sample()
    assert sample.run_statistics["duration_ms"] is None
    assert sample.run_statistics["tokens_in"] is None
    assert sample.run_statistics["tokens_out"] is None
    assert sample.run_statistics["llm_call_count"] == 2


def test_sample_unknown_schema_version_rejected() -> None:
    """未知 schema 版本必须明确失败。"""
    payload = make_sample().to_dict()
    payload["schema_version"] = "0.9"
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkSample.from_dict(payload)


def test_sample_field_type_error_rejected() -> None:
    """字段类型错误必须明确失败，不产出半成品对象。"""
    payload = make_sample().to_dict()
    payload["wall_duration_ms"] = "ten"
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkSample.from_dict(payload)

    payload = make_sample().to_dict()
    payload["passed"] = "yes"
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkSample.from_dict(payload)


def test_sample_failing_round_trip() -> None:
    """断言失败但 Trace 完整的样本可无损往返。"""
    sample = make_failing_sample()
    assert BenchmarkSample.from_dict(sample.to_dict()) == sample


def test_sample_none_optional_fields_round_trip() -> None:
    """可空字段（failure_kind / run_id / trace_source）为 None 时可往返。"""
    sample = make_sample(failure_kind=None, run_id=None, trace_source=None)
    restored = BenchmarkSample.from_dict(sample.to_dict())
    assert restored == sample
    assert restored.failure_kind is None
    assert restored.run_id is None
    assert restored.trace_source is None


# --------------------------------------------------------------------------- #
# 汇总结构：往返与严格校验
# --------------------------------------------------------------------------- #


def _latency_stats() -> LatencyStats:
    """构造时延分布汇总。"""
    return LatencyStats(sample_count=3, p50_ms=10.0, p95_ms=20.0, p99_ms=30.0, max_ms=31.0)


def test_latency_stats_round_trip() -> None:
    """时延分布汇总可无损往返。"""
    stats = _latency_stats()
    assert LatencyStats.from_dict(stats.to_dict()) == stats


def test_case_summary_round_trip() -> None:
    """Case 汇总可无损往返。"""
    summary = CaseSummary(
        case_id="tool_success",
        sample_count=3,
        passed_count=2,
        failed_count=1,
        success_rate=2 / 3,
        failure_kinds={"assertion": 1},
        wall_duration_ms=_latency_stats(),
        trace_metrics_ms={"critical_path_ms": _latency_stats()},
        llm_call_count_total=6,
        tool_call_count_total=3,
        trace_available_count=3,
        trace_missing_count=0,
        failed_tool_count_total=0,
    )
    assert CaseSummary.from_dict(summary.to_dict()) == summary


def test_global_summary_round_trip() -> None:
    """全局汇总可无损往返。"""
    summary = GlobalSummary(
        sample_count=6,
        passed_count=5,
        failed_count=1,
        success_rate=5 / 6,
        failure_kinds={"assertion": 1},
        wall_duration_ms=_latency_stats(),
        llm_call_count_total=12,
        tool_call_count_total=6,
        trace_available_count=6,
        trace_missing_count=0,
    )
    assert GlobalSummary.from_dict(summary.to_dict()) == summary


def test_summary_field_type_error_rejected() -> None:
    """汇总字段类型错误必须明确失败。"""
    payload = _latency_stats().to_dict()
    payload["p50_ms"] = None
    with pytest.raises(BenchmarkSchemaError):
        LatencyStats.from_dict(payload)


# --------------------------------------------------------------------------- #
# BenchmarkSnapshot：往返与严格校验
# --------------------------------------------------------------------------- #


def _snapshot() -> BenchmarkSnapshot:
    """构造完整快照。"""
    return BenchmarkSnapshot(
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
        repeat=30,
        cases=(CaseSummary(
            case_id="tool_success",
            sample_count=30,
            passed_count=30,
            failed_count=0,
            success_rate=1.0,
            wall_duration_ms=_latency_stats(),
        ),),
        global_summary=GlobalSummary(
            sample_count=30,
            passed_count=30,
            failed_count=0,
            success_rate=1.0,
            wall_duration_ms=_latency_stats(),
        ),
        samples_path="samples/20260806T091530Z_b6426cc.jsonl",
        samples_content_summary={"line_count": 35, "byte_count": 4096},
    )


def test_snapshot_to_dict_round_trip() -> None:
    """快照序列化后可按同一结构原样读回。"""
    snapshot = _snapshot()
    assert BenchmarkSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_snapshot_json_round_trip() -> None:
    """快照写出 JSON 后可重新读取。"""
    snapshot = _snapshot()
    payload = json.loads(json.dumps(snapshot.to_dict(), ensure_ascii=False))
    assert BenchmarkSnapshot.from_dict(payload) == snapshot


def test_snapshot_unknown_schema_version_rejected() -> None:
    """快照未知 schema 版本必须明确失败。"""
    payload = _snapshot().to_dict()
    payload["schema_version"] = "2.0"
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkSnapshot.from_dict(payload)


def test_snapshot_schema_version_constant() -> None:
    """默认 schema 版本必须与模块常量一致。"""
    assert _snapshot().schema_version == BENCHMARK_SCHEMA_VERSION
    assert make_sample().schema_version == BENCHMARK_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# PR2 来源元数据：execution_source / source_commit / scenario_id / evidence_kind
# --------------------------------------------------------------------------- #


def test_sample_source_metadata_round_trip() -> None:
    """历史来源元数据可无损往返。"""
    sample = make_sample(
        execution_source="historical_adapter",
        source_commit="4e4cdd3a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
        scenario_id="tool_success",
        evidence_kind="final_result",
        trace_available=False,
        run_id=None,
        trace_source=None,
    )
    restored = BenchmarkSample.from_dict(sample.to_dict())
    assert restored == sample
    assert restored.execution_source.value == "historical_adapter"
    assert restored.evidence_kind.value == "final_result"
    assert restored.source_commit == sample.source_commit


def test_sample_defaults_to_current_eval() -> None:
    """缺省来源字段默认为当前 Eval 链路。"""
    sample = make_sample()
    assert sample.execution_source.value == "current_eval"
    assert sample.evidence_kind.value == "run_trace"


def test_legacy_sample_without_source_fields_reads_with_defaults() -> None:
    """PR1 旧样本（无来源字段）读取时用默认值兼容，不破坏已提交基线。"""
    payload = make_sample().to_dict()
    for key in ("execution_source", "source_commit", "scenario_id", "evidence_kind"):
        payload.pop(key)
    restored = BenchmarkSample.from_dict(payload)
    assert restored.execution_source.value == "current_eval"
    assert restored.evidence_kind.value == "run_trace"
    assert restored.source_commit == ""
    assert restored.scenario_id == ""


def test_sample_unknown_execution_source_rejected() -> None:
    """未知执行来源取值必须明确失败。"""
    payload = make_sample().to_dict()
    payload["execution_source"] = "other_runtime"
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkSample.from_dict(payload)


def test_sample_unknown_evidence_kind_rejected() -> None:
    """未知证据类型取值必须明确失败。"""
    payload = make_sample().to_dict()
    payload["evidence_kind"] = "screenshot"
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkSample.from_dict(payload)


def test_snapshot_source_metadata_round_trip() -> None:
    """快照级执行来源与场景标识可无损往返。"""
    snapshot = _snapshot()
    payload = snapshot.to_dict()
    payload["execution_source"] = "historical_adapter"
    payload["scenario_id"] = "tool_success"
    restored = BenchmarkSnapshot.from_dict(payload)
    assert restored.execution_source.value == "historical_adapter"
    assert restored.scenario_id == "tool_success"


def test_legacy_snapshot_without_source_fields_reads_with_defaults() -> None:
    """PR1 旧快照（无来源字段）读取时用默认值兼容。"""
    payload = _snapshot().to_dict()
    payload.pop("execution_source")
    payload.pop("scenario_id")
    restored = BenchmarkSnapshot.from_dict(payload)
    assert restored.execution_source.value == "current_eval"
    assert restored.scenario_id == ""
