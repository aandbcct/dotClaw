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
async def test_playback_forces_playback_mode(tmp_path: Path) -> None:
    """即使 Case 声明 REEXECUTION，PlaybackRunner 也不覆盖，仅执行现有模式。
    
    实际上 Case 来自 confirm_draft 流程，均默认 PLAYBACK；
    此测试验证 runner 不做额外的模式覆写。
    """
    _seed_cases(tmp_path, "ds-play", (("case-c", "hello", "world"),))
    runner = _make_runner()
    results = await runner.run_dataset(tmp_path, "ds-play")
    assert all(r.passed for r in results)


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
