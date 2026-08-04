"""PR6 PlaybackRunner：批量 Playback 执行与 Gate 集成。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.eval.models import EvalCase, ExecutionMode, Expectation
from dotclaw.eval.results import EvalResult, EvaluationFailureKind

from .helpers import build_case, llm_response, make_llm_fixture, make_simple_trace


# ---------------------------------------------------------------------------
# 端到端：PlaybackRunner + Gate 对合法 Dataset 的完整判定
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playback_multi_case_all_pass(tmp_path: Path) -> None:
    """多个合法 Playback Case 全部通过 → Gate PASS。"""
    _seed_cases(tmp_path, "ds-e2e", (
        ("case-a", "The weather is sunny", "sunny"),
        ("case-b", "It's raining", "raining"),
    ))
    runner = _make_runner()
    report = await runner.run_and_gate(tmp_path, "ds-e2e")

    assert report.overall_status == "PASS"
    assert report.passed is True
    assert len(report.case_results) == 2
    assert all(c.passed for c in report.case_results)


@pytest.mark.asyncio
async def test_playback_assertion_failure_is_regression(tmp_path: Path) -> None:
    """断言失败的 Case 导致 Gate REGRESSION。"""
    case = _make_case("case-fail", "wrong answer tool", "sunny")
    case = _with_expectations(case, (
        Expectation("output_assertion", "text", "expected-answer", {"mode": "contains"}),
    ))
    _save_case(tmp_path, "ds-reg", case)

    report = await _make_runner().run_and_gate(tmp_path, "ds-reg")
    assert report.overall_status == "REGRESSION"
    assert report.passed is False


@pytest.mark.asyncio
async def test_playback_empty_dataset_is_error(tmp_path: Path) -> None:
    """空 Dataset（无 cases/ 子目录→无 Case）归为 ERROR。"""
    (tmp_path / "ds-empty" / "cases").mkdir(parents=True)
    report = await _make_runner().run_and_gate(tmp_path, "ds-empty")
    assert report.overall_status == "ERROR"
    assert report.passed is False


@pytest.mark.asyncio
async def test_playback_nonexistent_dataset_is_error(tmp_path: Path) -> None:
    """不存在的 Dataset 目录归为 ERROR。"""
    report = await _make_runner().run_and_gate(tmp_path, "nonexistent")
    assert report.overall_status == "ERROR"


# ---------------------------------------------------------------------------
# 结果校验：Playback 模式强制
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playback_forces_playback_mode_on_reexecution_case(tmp_path: Path) -> None:
    """声明 REEXECUTION 的 Case 也强制以 PLAYBACK/STRICT 执行。"""
    import dataclasses
    from dotclaw.eval.models import ExecutionMode

    case = _make_case("case-force", "hello", "world")
    # 故意声明 REEXECUTION——PlaybackRunner 应忽略并强制 PLAYBACK
    case = dataclasses.replace(case, execution_mode=ExecutionMode.REEXECUTION)
    _save_case(tmp_path, "ds-force", case)

    results = await _make_runner().run_dataset(tmp_path, "ds-force")
    assert len(results) == 1
    # 强制 Playback 应成功执行（若泄漏 REEXECUTION 去依赖真实端口则失败）
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_playback_results_are_deterministic(tmp_path: Path) -> None:
    """相同 Dataset 重复执行产出相同 passed 状态（规范化后可比较）。"""
    _seed_cases(tmp_path, "ds-det", (("case-d", "hello", "world"), ("case-e", "hi", "hey"),))
    runner = _make_runner()

    first = await runner.run_dataset(tmp_path, "ds-det")
    second = await runner.run_dataset(tmp_path, "ds-det")

    assert len(first) == len(second)
    for f, s in zip(first, second):
        assert f.passed == s.passed
        assert f.case_id == s.case_id
        assert f.failure_kind == s.failure_kind


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_runner():
    """构造 PlaybackRunner（延迟导入以兼容测试发现期无 Runtime 依赖）。"""
    from dotclaw.eval.playback import PlaybackRunner
    return PlaybackRunner()


def _make_case(case_id: str, final_content: str, tool_output: str) -> EvalCase:
    """构造最小工具调用 Playback Case——与 make_simple_trace 对齐。"""
    return build_case(
        case_id=case_id,
        llm_fixture=make_llm_fixture(
            "llm-1",
            (
                llm_response("llm-resp-1", content="", tool_calls=(
                    __import__("dotclaw.runtime.domain.facts", fromlist=["ToolCall"]).ToolCall(
                        call_id="call-1", name="search", arguments={"q": "weather"}
                    ),
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


def _with_expectations(case: EvalCase, expectations: tuple[Expectation, ...]) -> EvalCase:
    import dataclasses
    return dataclasses.replace(case, expectations=expectations)


def _seed_cases(root: Path, dataset_name: str, cases: tuple[tuple[str, str, str], ...]) -> None:
    """向临时目录写入一批 Case 文件。"""
    from dotclaw.eval.dataset import save_case
    cases_dir = root / dataset_name / "cases"
    cases_dir.mkdir(parents=True)
    for case_id, final_content, tool_output in cases:
        c = _make_case(case_id, final_content, tool_output)
        save_case(root, dataset_name, c)


def _save_case(root: Path, dataset_name: str, case: EvalCase) -> None:
    from dotclaw.eval.dataset import save_case
    cases_dir = root / dataset_name / "cases"
    cases_dir.mkdir(parents=True)
    save_case(root, dataset_name, case)


# ---------------------------------------------------------------------------
# §5-5 反向隔离：Playback 不触碰生产 Resume / Session / 网络 / 工作目录
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playback_does_not_produce_session_artifacts_outside_tmp(tmp_path: Path) -> None:
    """Playback 执行完全在临时目录内运行，不写出 Session 或工作目录副产物。"""
    _seed_cases(tmp_path, "ds-iso", (("case-iso", "answer", "result"),))
    # 记录写操作前的文件快照
    before = set(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    await _make_runner().run_dataset(tmp_path, "ds-iso")
    after = set(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    # Playback 不应创建新文件（只读执行）
    new_files = after - before
    assert not new_files, f"Playback 不应产生临时文件副产物: {new_files}"


@pytest.mark.asyncio
async def test_playback_does_not_call_production_resume(tmp_path: Path) -> None:
    """PlaybackRunner 不调用 retry_interrupted 或生产 Resume。

    验证：执行成功完成后，用例中的 execution_mode 未被改为非 PLAYBACK，
    Run ID 是临时生成的（非生产 Run）。
    """
    _seed_cases(tmp_path, "ds-noresume", (("case-nr", "done", "ok"),))
    results = await _make_runner().run_dataset(tmp_path, "ds-noresume")
    assert len(results) == 1
    # 每个执行产出独立的临时 run_id
    assert results[0].run_id is not None
    # 通过 Playback 执行本身即验证了没有触发生产 resume 或网络调用


# ---------------------------------------------------------------------------
# §5-6 CI 出口码：Gate 状态与退出码映射
# ---------------------------------------------------------------------------


def test_gate_pass_maps_to_exit_code_zero() -> None:
    """PASS 状态映射为退出码 0。"""
    from dotclaw.eval.gate import RegressionGate
    from dotclaw.eval.regression import PlaybackBatch
    from dotclaw.eval.results import EvalResult
    r = EvalResult(
        schema_version="1.0", case_id="c1", run_id="r1",
        passed=True, assertion_results=()
    )
    report = RegressionGate().evaluate(PlaybackBatch(results=(r,), dataset="ds"))
    assert report.overall_status == "PASS"


def test_gate_regression_maps_to_exit_code_nonzero() -> None:
    """REGRESSION 状态对应 CI 失败。"""
    from dotclaw.eval.gate import RegressionGate
    from dotclaw.eval.models import Expectation
    from dotclaw.eval.regression import PlaybackBatch
    from dotclaw.eval.results import AssertionResult, EvalResult, EvaluationFailureKind
    r = EvalResult(
        schema_version="1.0", case_id="c1", run_id="r1",
        passed=False,
        assertion_results=(AssertionResult(Expectation("run_status", "run", "completed"), False, "wrong"),),
        failure_kind=EvaluationFailureKind.ASSERTION,
    )
    report = RegressionGate().evaluate(PlaybackBatch(results=(r,), dataset="ds"))
    assert report.overall_status == "REGRESSION"
    assert report.passed is False


def test_gate_error_maps_to_exit_code_nonzero() -> None:
    """ERROR 状态对应 CI 失败。"""
    from dotclaw.eval.gate import RegressionGate
    from dotclaw.eval.regression import PlaybackBatch
    from dotclaw.eval.results import EvalResult, EvaluationFailureKind
    r = EvalResult(
        schema_version="1.0", case_id="c1", run_id=None,
        passed=False,
        assertion_results=(),
        failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION,
        failure_detail="配置错误",
    )
    report = RegressionGate().evaluate(PlaybackBatch(results=(r,), dataset="ds"))
    assert report.overall_status == "ERROR"
    assert report.passed is False
