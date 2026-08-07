"""tests/benchmarks 共享构造器：组装 BenchmarkSample 与合成 Trace 指标。"""

from __future__ import annotations

from typing import Mapping

from benchmarks.eval_baseline_models import BenchmarkSample, EvidenceKind, ExecutionSource, SUITE_NAME


def make_sample(
    case_id: str = "tool_success",
    attempt: int = 0,
    is_warmup: bool = False,
    passed: bool = True,
    wall_duration_ms: float = 10.0,
    failure_kind: str | None = None,
    trace_available: bool = True,
    **overrides,
) -> BenchmarkSample:
    """构造一条合法的 BenchmarkSample；可覆盖任意字段。"""
    base = dict(
        schema_version="2.0",
        suite=SUITE_NAME,
        dataset="runtime_core_v1",
        case_id=case_id,
        attempt=attempt,
        is_warmup=is_warmup,
        git_commit="b6426cc",
        python_version="3.13.5",
        platform="Windows",
        config_hash="cfg-hash-1",
        eval_schema_version="1.0",
        passed=passed,
        failure_kind=failure_kind,
        assertions_passed=5 if passed else 0,
        assertions_total=5,
        trace_available=trace_available,
        wall_duration_ms=wall_duration_ms,
        trace_metrics={
            "llm_duration_ms": 2,
            "tool_duration_ms": 1,
            "approval_wait_ms": 0,
            "longest_tool_duration_ms": 1,
            "failed_tool_count": 0,
            "incomplete_span_count": 0,
            "critical_path_ms": 2,
        },
        run_statistics={
            "duration_ms": None,
            "llm_call_count": 2,
            "tool_call_count": 1,
            "tokens_in": None,
            "tokens_out": None,
        },
        run_id="run-1",
        trace_source={
            "run_id": "run-1",
            "session_id": "s1",
            "schema_version": "1.0",
            "is_partial": False,
            "record_hash": "hash-1",
            "source_run_status": "completed",
            "source_event_sequence": 8,
            "source_message_sequence": 4,
            "source_context_version_count": 2,
            "assembled_at": "2026-08-06T09:15:30Z",
        },
    )
    base.update(overrides)
    # 允许测试以字符串直接表达来源枚举，构造时统一转为枚举取值
    if isinstance(base.get("execution_source"), str):
        base["execution_source"] = ExecutionSource(base["execution_source"])
    if isinstance(base.get("evidence_kind"), str):
        base["evidence_kind"] = EvidenceKind(base["evidence_kind"])
    return BenchmarkSample(**base)


def make_failing_sample(case_id: str = "tool_success", **overrides) -> BenchmarkSample:
    """构造断言失败但 Trace 完整的样本。"""
    base = dict(
        case_id=case_id,
        passed=False,
        failure_kind="assertion",
        assertions_passed=4,
        assertions_total=5,
    )
    base.update(overrides)
    return make_sample(**base)


def make_untrusted_sample(case_id: str = "tool_success", **overrides) -> BenchmarkSample:
    """构造不可信结果样本（如 Fixture 配置错误）。"""
    base = dict(
        case_id=case_id,
        passed=False,
        failure_kind="fixture_configuration",
        assertions_passed=0,
        assertions_total=0,
        trace_available=False,
        run_id=None,
        trace_source=None,
    )
    base.update(overrides)
    return make_sample(**base)
