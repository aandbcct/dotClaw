"""Runtime 组合根的 Agent Context Plan 覆盖测试。"""

from __future__ import annotations

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.runtime_factory import _agent_context_plan_configuration
from dotclaw.context import ContextPlanConfigurationPort
from dotclaw.runtime.domain.context import ContextOwner


def test_agent_identity_overrides_only_its_own_context_slots() -> None:
    """一个 Agent 的 Slot 配置不得改变 Session、Run、Global Owner 的有效计划。"""
    identity: AgentIdentity = AgentIdentity(
        agent_id="research-agent",
        context_slot_ids=("identity", "skills"),
    )
    configuration: ContextPlanConfigurationPort | None = _agent_context_plan_configuration(identity)

    assert configuration is not None
    assert configuration.enabled_slot_ids(ContextOwner.AGENT, identity.agent_id) == ("identity", "skills")
    assert configuration.enabled_slot_ids(ContextOwner.SESSION, "session-1") == ("user_info", "history")
    assert configuration.enabled_slot_ids(ContextOwner.RUN, "run-1") == ("memory", "knowledge", "run_messages")
    assert configuration.enabled_slot_ids(ContextOwner.GLOBAL, "global") == ("available_agents",)
