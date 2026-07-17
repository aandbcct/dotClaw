"""Runtime 重构 Phase 1、Phase 2 持久化边界验收测试。"""

from __future__ import annotations

from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, JSONMap, RunStatus


def test_phase2_runtime_v2_agent_run_is_summary_only() -> None:
    """v2 AgentRun 仅保存摘要，完整消息和恢复数据由独立容器持有。"""
    policy: AgentPolicySnapshot = AgentPolicySnapshot("agent-1", "identity-v1", "model-v1", 8)
    run: AgentRun = AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        status=RunStatus.RUNNING,
        started_at="2026-07-16T00:00:00+00:00",
        policy=policy,
        input_message_id="message-user-1",
    )
    serialized_run: JSONMap = run.to_dict()
    assert "messages" not in serialized_run
    assert "state_snapshot" not in serialized_run
    assert "trace_ids" not in serialized_run
