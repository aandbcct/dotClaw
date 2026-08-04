"""PR6 阶段性回归：RegressionReport 模型、结果规范化与 _is_trusted。"""

import pytest

from dotclaw.eval.models import Expectation
from dotclaw.eval.regression import (
    REPORT_SCHEMA_VERSION,
    RegressionCaseResult,
    RegressionReport,
    _is_trusted,
    normalize_result,
)
from dotclaw.eval.results import AssertionResult, EvalResult, EvaluationFailureKind


def _make_result(
    case_id: str = "case-1",
    passed: bool = True,
    failure_kind: EvaluationFailureKind | None = None,
    failure_detail: str | None = None,
    *,
    run_id: str = "run-1",
) -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id=run_id,
        passed=passed,
        assertion_results=(
            AssertionResult(Expectation("run_status", "run", "completed"), True, "ok"),
        ),
        failure_kind=failure_kind,
        failure_detail=failure_detail,
    )


# ---------------------------------------------------------------------------
# RegressionCaseResult
# ---------------------------------------------------------------------------


def test_case_result_from_passing_eval_result() -> None:
    """通过的 EvalResult 映射为通过摘要。"""
    result = _make_result()
    cr = RegressionCaseResult.from_result(result)
    assert cr.case_id == "case-1"
    assert cr.passed is True
    assert cr.failure_kind is None
    assert cr.failure_detail is None


def test_case_result_from_failing_eval_result() -> None:
    """断言失败的 EvalResult 保留失败分类与详情。"""
    result = _make_result(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, failure_detail="wrong")
    cr = RegressionCaseResult.from_result(result)
    assert cr.passed is False
    assert cr.failure_kind == "assertion"
    assert cr.failure_detail == "wrong"


def test_case_result_truncates_long_detail() -> None:
    """超过 500 字符的详情被截断。"""
    long_detail = "x" * 600
    result = _make_result(passed=False, failure_kind=EvaluationFailureKind.RUNTIME, failure_detail=long_detail)
    cr = RegressionCaseResult.from_result(result)
    assert cr.failure_detail is not None
    assert len(cr.failure_detail) <= 503  # 500 + "…"
    assert cr.failure_detail.endswith("…")


def test_case_result_to_dict() -> None:
    """序列化输出是 JSON 兼容的。"""
    result = _make_result(passed=False, failure_kind=EvaluationFailureKind.ASSERTION, failure_detail="wrong")
    cr = RegressionCaseResult.from_result(result)
    d = cr.to_dict()
    assert d["case_id"] == "case-1"
    assert d["passed"] is False
    assert d["failure_kind"] == "assertion"
    assert d["failure_detail"] == "wrong"


# ---------------------------------------------------------------------------
# RegressionReport
# ---------------------------------------------------------------------------


def test_report_construction_and_fields() -> None:
    """报告的构造与状态常量。
    
    注：报告状态使用字符串 "PASS"/"REGRESSION"/"ERROR"（非 StrEnum），
    因为 Status 是 Gate 的判定结果而非数据的结构化分类；用常量字符串
    同时满足“报告可序列化”和“数值稳定”的约束。
    """
    report = RegressionReport(
        schema_version=REPORT_SCHEMA_VERSION,
        dataset="ds-1",
        overall_status="PASS",
        case_results=(),
    )
    assert report.schema_version == REPORT_SCHEMA_VERSION
    assert report.dataset == "ds-1"
    assert report.overall_status == "PASS"
    assert report.passed is True
    assert report.error_detail is None


def test_report_to_dict() -> None:
    """报告可序列化为 JSON 兼容字典。"""
    cr = RegressionCaseResult.from_result(_make_result())
    report = RegressionReport(
        schema_version=REPORT_SCHEMA_VERSION,
        dataset="ds-1",
        overall_status="PASS",
        case_results=(cr,),
    )
    d = report.to_dict()
    assert d["schema_version"] == REPORT_SCHEMA_VERSION
    assert d["dataset"] == "ds-1"
    assert d["overall_status"] == "PASS"
    assert len(d["case_results"]) == 1
    assert d["case_results"][0]["case_id"] == "case-1"


# ---------------------------------------------------------------------------
# normalize_result
# ---------------------------------------------------------------------------


def test_normalize_result_strips_run_id() -> None:
    """规范化后的 run_id 被替换为固定哨兵。"""
    result = _make_result(run_id="abc-123")
    normalized = normalize_result(result)
    assert normalized.run_id == "<normalized>"
    assert normalized.case_id == result.case_id
    assert normalized.passed == result.passed
    assert normalized.failure_kind == result.failure_kind


def test_normalized_results_are_equal_regardless_of_run_id() -> None:
    """相同语义、不同 run_id 的两个结果规范化后完全等价。"""
    r1 = _make_result()
    r2 = _make_result(run_id="different-id")
    n1 = normalize_result(r1)
    n2 = normalize_result(r2)
    assert n1.run_id == n2.run_id
    assert n1.case_id == n2.case_id
    assert n1.passed == n2.passed


# ---------------------------------------------------------------------------
# _is_trusted
# ---------------------------------------------------------------------------


def test_passed_result_is_trusted() -> None:
    """通过的 EvalResult 是受信的。"""
    assert _is_trusted(_make_result(passed=True)) is True


def test_assertion_failure_is_trusted() -> None:
    """断言失败但执行可信，仍是受信结果。"""
    assert _is_trusted(_make_result(passed=False, failure_kind=EvaluationFailureKind.ASSERTION)) is True


def test_fixture_configuration_is_not_trusted() -> None:
    """FIXTURE_CONFIGURATION 是不可信的。"""
    assert _is_trusted(_make_result(failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION)) is False


def test_runtime_error_is_not_trusted() -> None:
    """RUNTIME 错误是不可信的。"""
    assert _is_trusted(_make_result(failure_kind=EvaluationFailureKind.RUNTIME)) is False


def test_trace_reconstruction_error_is_not_trusted() -> None:
    """TRACE_RECONSTRUCTION 错误是不可信的。"""
    assert _is_trusted(_make_result(failure_kind=EvaluationFailureKind.TRACE_RECONSTRUCTION)) is False
