"""Runtime 组合根的 Agent Context Plan 覆盖测试（开发计划阶段4 修改项1）。

由完整 AgentRegistry 构造 Context Plan 配置，保留默认 Owner 配置，并将每个
显式声明 ``context_slot_ids`` 的 Identity 精确覆盖到对应 Agent Owner 标识。
"""

from __future__ import annotations

from dotclaw.agent.identity import AgentIdentity
from dotclaw.context import build_context_plan_from_registry
from dotclaw.context import ContextPlanConfigurationPort
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.domain.context import ContextOwner


def _registry_with(*identities: AgentIdentity) -> AgentRegistry:
    registry: AgentRegistry = AgentRegistry()
    for identity in identities:
        registry.register(identity)
    return registry


def test_agent_identity_overrides_only_its_own_context_slots() -> None:
    """一个 Agent 的 Slot 配置不得改变 Session、Run、Global Owner 的有效计划。"""
    identity: AgentIdentity = AgentIdentity(
        agent_id="research-agent",
        context_slot_ids=("identity", "skills"),
    )
    configuration: ContextPlanConfigurationPort = build_context_plan_from_registry(
        _registry_with(identity)
    )

    assert configuration.enabled_slot_ids(ContextOwner.AGENT, identity.agent_id) == ("identity", "skills")
    assert configuration.enabled_slot_ids(ContextOwner.SESSION, "session-1") == ("user_info", "history_compressions")
    assert configuration.enabled_slot_ids(ContextOwner.RUN, "run-1") == ("conversation", "memory", "knowledge", "run_messages")
    assert configuration.enabled_slot_ids(ContextOwner.GLOBAL, "global") == ("available_agents",)


def test_unconfigured_identity_falls_back_to_default_agent_slots() -> None:
    """未声明 ``context_slot_ids`` 的 Identity 回退到默认 Agent 计划，不被他人覆盖影响。"""
    configured: AgentIdentity = AgentIdentity(
        agent_id="research-agent",
        context_slot_ids=("identity", "skills"),
    )
    default_identity: AgentIdentity = AgentIdentity(agent_id="general-agent")
    configuration: ContextPlanConfigurationPort = build_context_plan_from_registry(
        _registry_with(configured, default_identity)
    )

    assert configuration.enabled_slot_ids(ContextOwner.AGENT, "research-agent") == ("identity", "skills")
    assert configuration.enabled_slot_ids(ContextOwner.AGENT, "general-agent") == ("identity", "tools", "skills")
