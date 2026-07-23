"""CLI 提交语义的静态契约测试。"""

from __future__ import annotations

from pathlib import Path


def test_cli_uses_agent_v2_submission_and_engine_control_operations() -> None:
    """CLI 仅通过 Agent 门面提交普通消息、审批决定和取消请求。"""
    source = (Path(__file__).resolve().parents[2] / "src/dotclaw/main.py").read_text(encoding="utf-8")

    assert "agent.process(current_session, user_input, text_stream_port)" in source
    assert "agent.resolve_approval(approval_id, approved, text_stream_port)" in source
    assert "agent.cancel_run(args, \"用户通过 CLI 取消\")" in source
    assert "agent.retry_interrupted(args, text_stream_port)" in source
    assert "agent.abandon_interrupted(args)" in source
    assert "agent.model_id" in source
    assert "agent._resolve_model()" not in source
    assert "Runtime.run" not in source
    assert "await channel.print_markdown(final_answer)" in source
    assert "agent.has_streamed_final_answer" in source
    assert "await channel.stream(\"\\n\")" in source
