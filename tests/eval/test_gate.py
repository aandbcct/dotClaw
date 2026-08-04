"""PR6 Gate 三态判定与 CI 准入。"""

from dotclaw.eval.gate import RegressionGate
from dotclaw.eval.regression import PlaybackBatch, RegressionReport
from dotclaw.eval.results import EvalResult, EvaluationFailureKind, AssertionResult
from dotclaw.eval.models import Expectation


def _batch(*results: EvalResult, dataset: str = "ds") -> PlaybackBatch:
    """辅助构造 PlaybackBatch。"""
    return PlaybackBatch(results=results, dataset=dataset)


def _pass(case_id: str = "case-1") -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id="run-1",
        passed=True,
        assertion_results=(),
    )


def _assertion_fail(case_id: str = "case-2") -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id="run-2",
        passed=False,
        assertion_results=(AssertionResult(Expectation("run_status", "run", "completed"), False, "wrong"),),
        failure_kind=EvaluationFailureKind.ASSERTION,
        failure_detail="行为不符合预期",
    )


def _fixture_error(case_id: str = "case-3") -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id="run-3",
        passed=False,
        assertion_results=(),
        failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION,
        failure_detail="工具未匹配",
    )


def _runtime_error(case_id: str = "case-4") -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id="run-4",
        passed=False,
        assertion_results=(),
        failure_kind=EvaluationFailureKind.RUNTIME,
        failure_detail="boom",
    )


def _trace_error(case_id: str = "case-5") -> EvalResult:
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id="run-5",
        passed=False,
        assertion_results=(),
        failure_kind=EvaluationFailureKind.TRACE_RECONSTRUCTION,
        failure_detail="trace 不完整",
    )


# ---------------------------------------------------------------------------
# 三态判定
# ---------------------------------------------------------------------------


def test_all_pass_is_pass() -> None:
    report = RegressionGate().evaluate(_batch(_pass(), _pass()))
    assert report.overall_status == "PASS"
    assert report.passed is True
    assert report.error_detail is None


def test_single_assertion_failure_is_regression() -> None:
    report = RegressionGate().evaluate(_batch(_pass(), _assertion_fail()))
    assert report.overall_status == "REGRESSION"
    assert report.passed is False
    assert report.error_detail is None


def test_fixture_configuration_is_error() -> None:
    report = RegressionGate().evaluate(_batch(_fixture_error()))
    assert report.overall_status == "ERROR"
    assert report.error_detail is not None
    assert "case-3" in report.error_detail


def test_runtime_error_is_error() -> None:
    report = RegressionGate().evaluate(_batch(_runtime_error()))
    assert report.overall_status == "ERROR"


def test_trace_reconstruction_is_error() -> None:
    report = RegressionGate().evaluate(_batch(_trace_error()))
    assert report.overall_status == "ERROR"


def test_mixed_trusted_and_untrusted_is_error() -> None:
    report = RegressionGate().evaluate(_batch(_pass(), _fixture_error()))
    assert report.overall_status == "ERROR"
    assert len(report.case_results) == 2


def test_empty_results_is_error() -> None:
    report = RegressionGate().evaluate(PlaybackBatch(results=()))
    assert report.overall_status == "ERROR"
    assert report.error_detail is not None


# ---------------------------------------------------------------------------
# Report 结构完整性
# ---------------------------------------------------------------------------


def test_report_contains_all_case_summaries() -> None:
    report = RegressionGate().evaluate(_batch(_pass("c1"), _assertion_fail("c2"), _pass("c3")))
    assert len(report.case_results) == 3
    case_ids = {c.case_id for c in report.case_results}
    assert case_ids == {"c1", "c2", "c3"}


def test_report_dataset_field_is_preserved() -> None:
    report = RegressionGate().evaluate(_batch(_pass(), dataset="my-dataset"))
    assert report.dataset == "my-dataset"


def test_error_report_has_diagnostic_detail() -> None:
    report = RegressionGate().evaluate(_batch(_fixture_error("c-broken")))
    assert report.error_detail is not None
    assert "c-broken" in report.error_detail
