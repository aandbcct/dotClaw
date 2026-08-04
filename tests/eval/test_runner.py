"""EvalRunner 主链、四类失败互斥分类、多期望全通过/任一失败、重复性与端到端。"""


from dotclaw.eval.environment import EvalEnvironment
from dotclaw.eval.fixtures import FixtureConfigurationError
from dotclaw.eval.models import Expectation
from dotclaw.eval.results import EvaluationFailureKind
from dotclaw.eval.runner import EvalRunner
from dotclaw.eval.scorers import ALL_SCORERS, ExpectationKind, SCORERS
from dotclaw.eval.scorers._helpers import tool_spans
from .eval_testkit import approval_required_case, tool_case

PASSING_EXPECTATIONS = (
    Expectation("run_status", "outcome", "completed"),
    Expectation("tool_sequence", "tool_name", ["search"]),
    Expectation("tool_argument", "call-1", {"q": "weather"}),
    Expectation("output_assertion", "text", "sunny", {"mode": "contains"}),
    Expectation("iteration_budget", "llm_calls", 2),
)


async def test_all_expectations_pass():
    result = await EvalRunner().run(tool_case(expectations=PASSING_EXPECTATIONS))
    assert result.passed is True
    assert result.failure_kind is None
    assert result.run_id is not None
    assert result.trace is not None
    assert len(result.assertion_results) == len(PASSING_EXPECTATIONS)
    assert all(ar.passed for ar in result.assertion_results)


async def test_any_expectation_fail_is_assertion():
    expectations = PASSING_EXPECTATIONS + (Expectation("run_status", "outcome", "failed"),)
    result = await EvalRunner().run(tool_case(expectations=expectations))
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.ASSERTION
    assert any(not ar.passed for ar in result.assertion_results)


async def test_assertion_failure_evidence_traceable_to_trace():
    """断言失败时，结果必须携带可回溯到 Eval Run Trace 的证据。"""
    expectations = (
        Expectation("run_status", "outcome", "failed"),
        Expectation("tool_sequence", "tool_name", ["not_exist"]),
    )
    result = await EvalRunner().run(tool_case(expectations=expectations))
    assert result.failure_kind is EvaluationFailureKind.ASSERTION

    # 失败结果仍保留 Run 标识与完整 Trace，供逐条证据回溯
    assert result.run_id is not None
    assert result.trace is not None
    assert result.trace.run.run_id == result.run_id

    failed = [ar for ar in result.assertion_results if not ar.passed]
    assert len(failed) == 2
    for assertion in failed:
        assert assertion.evidence.strip()  # 每条失败都必须有非空证据

    # 证据描述的事实与 Trace 内实际记录一致：真实工具序列可在 Trace 中查到
    actual_tools = [
        str(span.attributes.get("tool_name", "")) for span in tool_spans(result.trace)
    ]
    assert actual_tools == ["search"]
    sequence_evidence = next(
        a.evidence for a in failed if a.expectation.kind == "tool_sequence"
    )
    assert "search" in sequence_evidence


async def test_fixture_configuration_unknown_kind():
    result = await EvalRunner().run(tool_case(expectations=(Expectation("bogus_kind", "x", "y"),)))
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert result.run_id is None
    assert result.trace is None


async def test_fixture_configuration_illegal_regex():
    expectations = (Expectation("output_assertion", "text", "[", {"mode": "regex"}),)
    result = await EvalRunner().run(tool_case(expectations=expectations))
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert result.run_id is None
    assert result.trace is None


async def test_fixture_configuration_on_env_run_error(monkeypatch):
    async def _cfg_err(self, *args, **kwargs):
        raise FixtureConfigurationError("未匹配的 fixture")

    monkeypatch.setattr(EvalEnvironment, "run", _cfg_err)
    result = await EvalRunner().run(tool_case(expectations=PASSING_EXPECTATIONS))
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert result.run_id is None
    assert result.trace is None


async def test_runtime_error_classification(monkeypatch):
    async def _boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(EvalEnvironment, "run", _boom)
    result = await EvalRunner().run(tool_case(expectations=PASSING_EXPECTATIONS))
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.RUNTIME
    assert result.run_id is None
    assert result.trace is None


async def test_trace_reconstruction_assemble_error(monkeypatch):
    def _broken(*args, **kwargs):
        raise ValueError("broken trace")

    monkeypatch.setattr("dotclaw.eval.runner.assemble_trace", _broken)
    result = await EvalRunner().run(tool_case())
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.TRACE_RECONSTRUCTION
    assert result.run_id is not None
    assert result.trace is None


async def test_trace_reconstruction_partial_without_allow():
    result = await EvalRunner().run(approval_required_case())
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.TRACE_RECONSTRUCTION
    assert result.run_id is not None
    assert result.trace is not None


async def test_partial_trace_allow_flag_enables_scoring():
    case = approval_required_case()
    object.__setattr__(case, "allow_partial_trace", True)
    object.__setattr__(
        case,
        "expectations",
        (Expectation("run_status", "outcome", "suspended"),),
    )
    result = await EvalRunner().run(case)
    assert result.passed is True
    assert result.failure_kind is None


async def test_determinism_repeatable():
    first = await EvalRunner().run(tool_case(expectations=PASSING_EXPECTATIONS))
    second = await EvalRunner().run(tool_case(expectations=PASSING_EXPECTATIONS))
    fingerprint = lambda r: (
        r.passed,
        r.failure_kind.value if r.failure_kind else None,
        tuple((a.expectation.kind, a.expectation.target, a.passed, a.evidence) for a in r.assertion_results),
    )
    assert first.run_id != second.run_id  # 运行标识每次不同
    assert fingerprint(first) == fingerprint(second)  # 但评分结果等价


def test_scorer_registry_covers_all_kinds():
    assert len(ALL_SCORERS) == 9
    assert set(SCORERS.keys()) == {kind for kind in ExpectationKind}
    for scorer in SCORERS.values():
        assert hasattr(scorer, "KIND")
        assert scorer.KIND in ExpectationKind
