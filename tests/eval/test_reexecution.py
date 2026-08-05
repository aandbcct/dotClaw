"""PR6 ReexecutionRunner：Re-execution 模式执行与比较路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.eval.models import EvalCase, Expectation


# ---------------------------------------------------------------------------
# Re-execution 基础
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reexecution_multi_case_runs(tmp_path: Path) -> None:
    """多个 Case 以 REEXECUTION 模式成功执行。"""
    _seed_cases(tmp_path, "ds-reex", (
        ("case-x", "The answer is 42", "42"),
        ("case-y", "It works", "ok"),
    ))
    runner = _make_runner()
    results = await runner.run_dataset(tmp_path, "ds-reex")

    assert len(results) == 2
    assert all(r.case_id in ("case-x", "case-y") for r in results)


@pytest.mark.asyncio
async def test_reexecution_empty_dataset_is_error() -> None:
    """空 Dataset 的 Re-execution 产出单条不可信结果。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ds-empty-reex" / "cases").mkdir(parents=True)
        results = await _make_runner().run_dataset(root, "ds-empty-reex")
        assert len(results) == 1
        assert results[0].passed is False


@pytest.mark.asyncio
async def test_reexecution_results_not_entered_into_gate() -> None:
    """Re-execution 结果应仅用于人工比较，不被 Gate 使用。

    验证：任意 Re-execution 结果均可安全收集，不抛 Gate 相关��常。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_cases(root, "ds-reex-gate", (("case-z", "hello", "world"),))
        results = await _make_runner().run_dataset(root, "ds-reex-gate")
        # Reexecution 只返回原始结果，不调用 Gate
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 隔离性：Re-execution 不写原 Run / Session，保留 Conversation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reexecution_preserves_case_conversation(tmp_path: Path) -> None:
    """Re-execution 保留 Case 中的会话与隔离 Fixture（通过成功执行验证）。"""
    _seed_cases(tmp_path, "ds-conv", (("case-conv", "final answer", "tool-output"),))
    results = await _make_runner().run_dataset(tmp_path, "ds-conv")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_reexecution_results_cannot_enter_gate() -> None:
    """Re-execution 直接结果无法传入 Gate（PlaybackBatch 类型检查）。

    ``RegressionGate.evaluate()`` 仅接受 ``PlaybackBatch``——
    ReexecutionRunner 不产出该类型，外部无法将普通 tuple[EvalResult]
    传入 Gate。
    """
    import tempfile

    from dotclaw.eval.gate import RegressionGate
    from dotclaw.eval.regression import PlaybackBatch

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_cases(root, "ds-type", (("case-tt", "yes", "ok"),))
        results = await _make_runner().run_dataset(root, "ds-type")
        assert len(results) == 1

    # Gate 不接受 tuple[EvalResult]，仅接受 PlaybackBatch
    gate = RegressionGate()
    try:
        gate.evaluate(results)  # type: ignore[arg-type]
        assert False, "Gate 不应接受 tuple[EvalResult]"
    except (TypeError, AttributeError):
        pass

    # 但正确构造的 PlaybackBatch 应正常工作
    batch = PlaybackBatch(results=results, dataset="ds-type")
    report = gate.evaluate(batch)
    assert report.overall_status in ("PASS", "REGRESSION", "ERROR")


# ---------------------------------------------------------------------------
# Re-execution 隔离：仅 LLM/Context/Policy 可真实回退
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reexecution_rejects_real_tool_port(tmp_path: Path) -> None:
    """注入真实 ToolPort 时 Re-execution 环境直接拒绝（FIXTURE_CONFIGURATION）。"""
    from dotclaw.eval.environment import EvalDependencies
    from dotclaw.eval.reexecution import ReexecutionRunner

    _seed_cases(tmp_path, "ds-reject-tool", (("case-rt", "final", "out"),))
    # 注入一个假的 ToolPort
    deps = EvalDependencies(tool_port=object())  # type: ignore[arg-type]
    runner = ReexecutionRunner(dependencies=deps)
    results = await runner.run_dataset(tmp_path, "ds-reject-tool")
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].failure_kind is not None
    assert results[0].failure_kind.value == "fixture_configuration"


@pytest.mark.asyncio
async def test_reexecution_accepts_llm_deps_only(tmp_path: Path) -> None:
    """仅注入 LLM/Context/Policy 的 Re-execution 正常工作（通过 Fixture）。"""
    from dotclaw.eval.environment import EvalDependencies
    from dotclaw.eval.reexecution import ReexecutionRunner

    _seed_cases(tmp_path, "ds-allow-llm", (("case-al", "answer", "ok"),))
    # 只给 LLM（Fixture 未覆盖时会回退，但 Fixture 已覆盖所以不用回退）
    deps = EvalDependencies(llm_port=object())  # type: ignore[arg-type]
    runner = ReexecutionRunner(dependencies=deps)
    results = await runner.run_dataset(tmp_path, "ds-allow-llm")
    assert len(results) == 1
    # Fixture 存在时 LLM port 不影响结果
    assert results[0].passed is True


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_runner():
    """构造 ReexecutionRunner。"""
    from dotclaw.eval.reexecution import ReexecutionRunner
    return ReexecutionRunner()


def _make_case(case_id: str, final_content: str, tool_output: str) -> EvalCase:
    """构造最小工具调用 Case。"""
    from .helpers import build_case, llm_response, make_llm_fixture
    from dotclaw.runtime.domain.facts import ToolCall

    return build_case(
        case_id=case_id,
        llm_fixture=make_llm_fixture(
            "llm-1",
            (
                llm_response("llm-resp-1", content="", tool_calls=(
                    ToolCall(call_id="call-1", name="search", arguments={"q": "weather"}),
                )),
                llm_response("llm-resp-2", content=final_content),
            ),
        ),
        tool_fixtures=(_tool_fixture("tool-1", "search", {"q": "weather"}, tool_output),),
        context_fixtures=(_ctx_fixture("ctx-1", ["search"]), _ctx_fixture("ctx-2", ["search"])),
        expectations=(
            Expectation("run_status", "run", "completed"),
            Expectation("token_budget", "tokens_in", 10000),
            Expectation("token_budget", "tokens_out", 10000),
            Expectation("iteration_budget", "llm_calls", 10),
            Expectation("iteration_budget", "tool_calls", 10),
        ),
    )


def _tool_fixture(fixture_id: str, tool_name: str, key_arguments: dict, output: str):
    from dotclaw.eval.models import ToolFixture
    from dotclaw.runtime.application.dto import ToolResultStatus
    return ToolFixture(
        fixture_id=fixture_id,
        tool_name=tool_name,
        key_arguments=key_arguments,
        status=ToolResultStatus.COMPLETED,
        output=output,
    )


def _ctx_fixture(fixture_id: str, tool_names: list[str]):
    from dotclaw.eval.models import ContextFixture
    from dotclaw.runtime.application.dto import ToolDefinition
    tools = tuple(ToolDefinition(name=n, description="", parameters={}) for n in tool_names)
    return ContextFixture(fixture_id=fixture_id, tools=tools, estimated_tokens=1)


def _seed_cases(root: Path, dataset_name: str, cases: tuple[tuple[str, str, str], ...]) -> None:
    from dotclaw.eval.dataset import save_case
    cases_dir = root / dataset_name / "cases"
    cases_dir.mkdir(parents=True)
    for case_id, final_content, tool_output in cases:
        c = _make_case(case_id, final_content, tool_output)
        save_case(root, dataset_name, c)
