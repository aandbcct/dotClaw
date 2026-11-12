"""dotClaw CLI 启动参数测试。"""

from __future__ import annotations

from dotclaw.main import _parse_show_reasoning
from dotclaw.bootstrap.session_interaction import format_run_result
from dotclaw.runtime.application.dto import RunResult
from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running


def test_show_reasoning_by_default() -> None:
    """未提供开关时默认展示思考过程。"""
    show_reasoning, ci_dataset = _parse_show_reasoning([])
    assert show_reasoning is True
    assert ci_dataset is None


def test_hide_thinking_disables_reasoning_display() -> None:
    """指定隐藏开关后，CLI 不展示思考过程。"""
    show_reasoning, _ = _parse_show_reasoning(["--hide-thinking"])
    assert show_reasoning is False


def test_cli_distinguishes_cancellation_drain_and_recoverable_tool_result() -> None:
    state = AgentRunState(mode=Running(RunStage.EXECUTING_TOOLS))

    assert "正在停止" in format_run_result(RunResult("run-cancel", state, control_status="cancelling"))
    assert "先核对外部操作" in format_run_result(RunResult("run-unknown", state, control_status="recoverable"))
