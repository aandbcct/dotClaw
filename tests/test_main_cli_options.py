"""dotClaw CLI 启动参数测试。"""

from __future__ import annotations

from dotclaw.main import _parse_show_reasoning


def test_show_reasoning_by_default() -> None:
    """未提供开关时默认展示思考过程。"""
    show_reasoning, ci_dataset = _parse_show_reasoning([])
    assert show_reasoning is True
    assert ci_dataset is None


def test_hide_thinking_disables_reasoning_display() -> None:
    """指定隐藏开关后，CLI 不展示思考过程。"""
    show_reasoning, _ = _parse_show_reasoning(["--hide-thinking"])
    assert show_reasoning is False
