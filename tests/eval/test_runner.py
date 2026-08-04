"""EvalRunner 主链、四类失败互斥分类、多期望全通过/任一失败、重复性与端到端。"""


from dotclaw.eval.environment import EvalEnvironment
from dotclaw.eval.fixtures import FixtureConfigurationError
from dotclaw.eval.models import Expectation
from dotclaw.eval.results import EvaluationFailureKind
from dotclaw.eval.runner import EvalRunner
from dotclaw.eval.scorers import ALL_SCORERS, ExpectationKind, SCORERS, Scorer
from dotclaw.eval.scorers._helpers import approval_spans, tool_spans
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.trace.models import TraceSpanStatus
from .eval_testkit import approval_required_case, approval_resolved_case, tool_case
from .helpers import approval_fixture, llm_response, make_llm_fixture, tool_call, tool_fixture

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


async def test_fixture_configuration_unconsumed_llm_fixture():
    """Run 完整结束却剩下没被调用的 LLM Fixture：Case 声明与执行不一致，结果不可信。"""
    case = tool_case(
        case_id="extra-llm-fixture",
        llm_fixture=make_llm_fixture(
            "llm-1",
            (
                llm_response("llm-resp-1", content="", tool_calls=(tool_call("call-1", "search", {"q": "weather"}),)),
                llm_response("llm-resp-2", content="The weather is sunny today"),
                llm_response("llm-resp-3-unused", content="本次执行不会用到"),
            ),
        ),
        expectations=PASSING_EXPECTATIONS,
    )
    result = await EvalRunner().run(case)
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert "llm-resp-3-unused" in result.failure_detail
    # 未消费属配置错误而非断言失败：不产出任何断言明细
    assert result.assertion_results == ()
    # 已执行过，证据仍可回溯到本次运行
    assert result.run_id is not None
    assert result.trace is not None


async def test_fixture_configuration_unconsumed_tool_fixture():
    """多余的工具 Fixture 同样归为配置错误，与缺失 Fixture 同类。"""
    case = tool_case(
        case_id="extra-tool-fixture",
        tool_fixtures=(
            tool_fixture("tool-1", "search", key_arguments={"q": "weather"}, output="sunny"),
            tool_fixture("tool-2-unused", "translate", key_arguments={"q": "hello"}, output="你好"),
        ),
        expectations=PASSING_EXPECTATIONS,
    )
    result = await EvalRunner().run(case)
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert "tool-2-unused" in result.failure_detail


async def test_unconsumed_fixture_not_reported_for_partial_trace():
    """挂起产生的部分 Trace 必然剩余 Fixture，应归 TRACE_RECONSTRUCTION 而非配置错误。"""
    result = await EvalRunner().run(approval_required_case())
    assert result.failure_kind is EvaluationFailureKind.TRACE_RECONSTRUCTION
    assert result.failure_kind is not EvaluationFailureKind.FIXTURE_CONFIGURATION


# --------------------------------------------------------------------------- #
# 审批 Fixture 驱动隔离 Run：自动批准 / 自动拒绝 / 按预期停在等待
# --------------------------------------------------------------------------- #


async def test_approval_fixture_auto_approves_isolated_run():
    """声明 approved 决议时，隔离 Run 应被自动批准并继续跑到完成终态。"""
    expectations = (
        Expectation("run_status", "outcome", "completed"),
        Expectation("approval", "apr-1", "approved"),
        Expectation("output_assertion", "text", "sunny", {"mode": "contains"}),
    )
    result = await EvalRunner().run(approval_resolved_case(approved=True, expectations=expectations))
    assert result.failure_kind is None
    assert result.passed is True
    assert result.trace is not None
    # 自动决议后运行已真正结束，Trace 不再是部分重建
    assert result.trace.source.is_partial is False
    assert result.trace.run.state.outcome() is RunOutcome.COMPLETED

    approval = approval_spans(result.trace)
    assert len(approval) == 1
    assert approval[0].attributes.get("approval_id") == "apr-1"
    assert approval[0].status is TraceSpanStatus.COMPLETED
    assert approval[0].attributes.get("approved") is True
    # 审批通过后引擎重放同一工具调用：先有等待中的一次，再有真正执行完成的一次
    assert [span.status for span in tool_spans(result.trace)] == [
        TraceSpanStatus.WAITING,
        TraceSpanStatus.COMPLETED,
    ]


async def test_approval_fixture_auto_rejects_isolated_run():
    """声明 rejected 决议时，隔离 Run 应被自动拒绝并以取消终态收口。"""
    expectations = (
        Expectation("run_status", "outcome", "cancelled"),
        Expectation("approval", "apr-1", "rejected"),
    )
    result = await EvalRunner().run(approval_resolved_case(approved=False, expectations=expectations))
    assert result.failure_kind is None
    assert result.passed is True
    assert result.trace is not None
    assert result.trace.source.is_partial is False
    assert result.trace.run.state.outcome() is RunOutcome.CANCELLED

    assert approval_spans(result.trace)[0].status is TraceSpanStatus.CANCELLED
    # 被拒绝的工具不会执行：只留下等待审批的那一个 Span
    assert [span.status for span in tool_spans(result.trace)] == [TraceSpanStatus.WAITING]


async def test_approval_fixture_consumed_by_isolated_run():
    """审批决议必须真的由隔离 Run 取用，而不是只在单测里直接调用 Fixture。"""
    env = EvalEnvironment(approval_resolved_case(approved=True))
    outcome = await env.run()
    assert env.fixture_approval.remaining == ()  # 审批 Fixture 已被主链消费
    assert outcome.result.state.is_ended()
    assert outcome.result.approval_id is None  # 不再挂在等待审批上
    outcome.assert_fully_consumed()  # 上下文 / LLM / 工具 / 审批 Fixture 均被用到


async def test_run_without_approval_fixture_stops_at_waiting():
    """未声明审批 Fixture 时按预期停在等待状态，并可在允许部分 Trace 下被断言。"""
    case = approval_required_case()
    object.__setattr__(case, "allow_partial_trace", True)
    object.__setattr__(
        case,
        "expectations",
        (
            Expectation("run_status", "outcome", "suspended"),
            Expectation("approval", "apr-1", "waiting"),
        ),
    )
    result = await EvalRunner().run(case)
    assert result.passed is True
    assert result.failure_kind is None
    assert result.trace.run.state.is_waiting_approval()


async def test_unused_approval_fixture_is_configuration_error():
    """声明了审批 Fixture 但 Run 从未请求审批：属多余 Fixture 配置错误。"""
    case = tool_case(
        case_id="extra-approval-fixture",
        approval_fixtures=(approval_fixture("apr-fix-unused", approved=True),),
        expectations=PASSING_EXPECTATIONS,
    )
    result = await EvalRunner().run(case)
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert "apr-fix-unused" in result.failure_detail


async def test_approval_fixture_id_mismatch_is_configuration_error():
    """审批 Fixture 指定的 approval_id 与实际请求不符：配置错误而非断言失败。"""
    case = approval_resolved_case(
        approved=True,
        approval_fixtures=(approval_fixture("apr-fix-1", approved=True, approval_id="apr-other"),),
    )
    result = await EvalRunner().run(case)
    assert result.passed is False
    assert result.failure_kind is EvaluationFailureKind.FIXTURE_CONFIGURATION
    assert "apr-other" in result.failure_detail


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


def test_all_scorers_satisfy_scorer_protocol():
    """九个 Scorer 均需结构化满足 Scorer 协议（KIND + score）。"""
    for kind, scorer in SCORERS.items():
        assert isinstance(scorer, Scorer)
        assert scorer.KIND is kind  # 注册表键与自身 KIND 一致
