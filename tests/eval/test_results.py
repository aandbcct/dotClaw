"""EvalResult / AssertionResult / EvaluationFailureKind 模型测试。"""


from dotclaw.eval.models import Expectation, SCHEMA_VERSION
from dotclaw.eval.results import (
    AssertionResult,
    EvalResult,
    EvaluationFailureKind,
)


def _expectation():
    return Expectation("run_status", "outcome", "completed")


def test_failure_kind_values():
    assert EvaluationFailureKind.RUNTIME.value == "runtime"
    assert EvaluationFailureKind.TRACE_RECONSTRUCTION.value == "trace_reconstruction"
    assert EvaluationFailureKind.FIXTURE_CONFIGURATION.value == "fixture_configuration"
    assert EvaluationFailureKind.ASSERTION.value == "assertion"


def test_assertion_result_to_dict():
    ar = AssertionResult(_expectation(), True, "ok")
    d = ar.to_dict()
    assert d["kind"] == "run_status"
    assert d["target"] == "outcome"
    assert d["passed"] is True
    assert d["evidence"] == "ok"


def test_eval_result_passed_to_dict_excludes_trace_content():
    ar = AssertionResult(_expectation(), True, "ok")
    result = EvalResult(
        schema_version=SCHEMA_VERSION,
        case_id="c1",
        run_id="r1",
        passed=True,
        assertion_results=(ar,),
        failure_kind=None,
        failure_detail=None,
        trace=None,
    )
    d = result.to_dict()
    assert d["passed"] is True
    assert d["failure_kind"] is None
    assert d["case_id"] == "c1"
    assert d["run_id"] == "r1"
    assert d["trace_available"] is False
    assert "trace" not in d
    assert len(d["assertion_results"]) == 1


def test_eval_result_failure_to_dict():
    result = EvalResult(
        schema_version=SCHEMA_VERSION,
        case_id="c1",
        run_id=None,
        passed=False,
        assertion_results=(),
        failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION,
        failure_detail="bad",
        trace=None,
    )
    d = result.to_dict()
    assert d["passed"] is False
    assert d["failure_kind"] == "fixture_configuration"
    assert d["failure_detail"] == "bad"
    assert d["trace_available"] is False
