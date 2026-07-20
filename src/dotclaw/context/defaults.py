"""内置 Context Slot 的声明式注册与组合根辅助函数。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import ContextContributionKind, ContextOwner

from .contracts import ContextCacheScope, ContextRefreshPolicy, ContextSlotDescriptor
from .plan_configuration import ContextOwnerPlanConfiguration, InMemoryContextPlanConfiguration
from .plan_resolver import ContextPlanResolver
from .ports import ContextDependencies
from .provider import ContextProvider
from .registry import ContextSlotRegistry
from .signals import ContextSignalBus
from .slot_manager import ContextSlotManager
from .slots import (
    AvailableAgentsSlot,
    HistorySlot,
    IdentitySlot,
    KnowledgeSlot,
    MemorySlot,
    RunMessagesSlot,
    SkillsSlot,
    ToolsSlot,
    UserInfoSlot,
)


def build_context_provider(dependencies: ContextDependencies) -> ContextProvider:
    """装配内置注册表、解析器、生命周期管理器和 ContextPort。"""
    registry: ContextSlotRegistry = ContextSlotRegistry()
    _register_defaults(registry)
    signal_bus: ContextSignalBus = ContextSignalBus()
    manager: ContextSlotManager = ContextSlotManager(registry, signal_bus)
    configuration: InMemoryContextPlanConfiguration = default_context_plan_configuration()
    resolver: ContextPlanResolver = ContextPlanResolver(
        registry,
        dependencies.plan_configuration or configuration,
    )
    return ContextProvider(resolver, manager, dependencies)


def default_context_plan_configuration() -> InMemoryContextPlanConfiguration:
    """提供兼容现有行为的 Owner 默认配置，具体 Agent 可由 Port 覆盖。"""
    return InMemoryContextPlanConfiguration((
        ContextOwnerPlanConfiguration(ContextOwner.AGENT, ("identity", "tools", "skills")),
        ContextOwnerPlanConfiguration(ContextOwner.GLOBAL, ("available_agents",)),
        ContextOwnerPlanConfiguration(ContextOwner.SESSION, ("user_info", "history")),
        ContextOwnerPlanConfiguration(ContextOwner.RUN, ("memory", "knowledge", "run_messages")),
    ))


def _register_defaults(registry: ContextSlotRegistry) -> None:
    """注册内置 Slot；新增 Slot 只需在此组合根声明并由 Agent 启用。"""
    registry.register(_descriptor("identity", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 10), IdentitySlot)
    registry.register(_descriptor("tools", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 20), ToolsSlot)
    registry.register(_descriptor("skills", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 30), SkillsSlot)
    registry.register(_descriptor("available_agents", ContextOwner.GLOBAL, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.NONE, 40), AvailableAgentsSlot)
    registry.register(_descriptor("user_info", ContextOwner.SESSION, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.SESSION, 50), UserInfoSlot)
    registry.register(_descriptor("history", ContextOwner.SESSION, ContextContributionKind.HISTORY, ContextCacheScope.SESSION, 60), HistorySlot)
    registry.register(_descriptor("memory", ContextOwner.RUN, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.RUN, 70), MemorySlot)
    registry.register(_descriptor("knowledge", ContextOwner.RUN, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.RUN, 80), KnowledgeSlot)
    registry.register(_descriptor("run_messages", ContextOwner.RUN, ContextContributionKind.RUN_MESSAGE_REFERENCES, ContextCacheScope.RUN, 90), RunMessagesSlot)


def _descriptor(
    slot_id: str,
    owner: ContextOwner,
    contribution_kind: ContextContributionKind,
    cache_scope: ContextCacheScope,
    order: int,
) -> ContextSlotDescriptor:
    """构造固定采用信号刷新的内置 Slot 描述符。"""
    return ContextSlotDescriptor(slot_id, owner, contribution_kind, cache_scope, ContextRefreshPolicy.SIGNAL, order)
