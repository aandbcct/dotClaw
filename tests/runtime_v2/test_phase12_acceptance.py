"""Runtime 重构 Phase 1、Phase 2 收口验收测试。"""

from __future__ import annotations

from dotclaw.runtime.agent_state import (
    LegacyAgentAction,
    LegacyAgentPhase,
    LegacyAgentState,
    V2AgentAction,
    V2AgentPhase,
    V2AgentState,
)
from dotclaw.runtime.domain.models import AgentAction, AgentPolicySnapshot, AgentRun, JSONMap, RunStatus
from dotclaw.runtime.domain.state import AgentPhase, AgentState
from dotclaw.session.agent_run import AgentRun as OldAgentRun
from dotclaw.session.agent_run import LegacyAgentRun, LegacyAgentRunManager
from dotclaw.session.agent_run import AgentRunManager


def test_phase1_legacy_agent_state_exports_v2_and_legacy_boundaries() -> None:
    """旧状态机模块明确提供旧兼容别名和 v2 纯领域状态机入口。"""
    assert V2AgentState is AgentState
    assert V2AgentPhase is AgentPhase
    assert V2AgentAction is AgentAction
    assert LegacyAgentState.__module__ == "dotclaw.runtime.agent_state"
    assert LegacyAgentPhase.__module__ == "dotclaw.runtime.agent_state"
    assert LegacyAgentAction.__module__ == "dotclaw.runtime.agent_state"


def test_phase2_legacy_agent_run_aliases_remain_read_compatibility_only() -> None:
    """旧 AgentRun 仍有兼容名称，但 v2 领域摘要是独立类型。"""
    assert LegacyAgentRun is OldAgentRun
    assert LegacyAgentRun is not AgentRun
    assert LegacyAgentRunManager is AgentRunManager
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
