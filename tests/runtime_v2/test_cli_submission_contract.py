"""CLI 提交语义的静态契约测试。"""

from __future__ import annotations

from pathlib import Path


def test_cli_uses_service_entry_and_returns_run_result() -> None:
    """CLI 仅通过 SessionInteractionService 提交普通消息、审批决定、取消与重试/放弃。"""
    source = (Path(__file__).resolve().parents[2] / "src/dotclaw/main.py").read_text(encoding="utf-8")

    assert "service.submit(current_session, user_input, text_stream_port)" in source
    assert "service.resolve_approval(result.approval_id, approved, text_stream_port)" in source
    assert "service.cancel(args, \"用户通过 CLI 取消\")" in source
    assert "service.retry_interrupted(args, text_stream_port)" in source
    assert "service.abandon_interrupted(args)" in source
    assert "service.get_identity(" in source
    assert "Runtime.run" not in source
    assert "channel.print_markdown(" in source
    assert "has_streamed_text" in source
    assert "await channel.stream(\"\\n\")" in source
    # 不得重新引入运行时 Agent 门面。
    assert "agent.process(" not in source
    assert "from dotclaw.agent import Agent" not in source
