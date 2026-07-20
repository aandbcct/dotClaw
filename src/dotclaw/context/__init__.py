"""Runtime v3 的多 Owner 结构化上下文实现。"""

from dotclaw.runtime.application.dto import ContextMetadata
from dotclaw.runtime.application.ports import ContextPort

from .contracts import (
    ContextCacheScope,
    ContextContribution,
    ContextOwnerSnapshot,
    ContextPlan,
    ContextRefreshPolicy,
    ContextSlot,
    ContextSlotBinding,
    ContextSlotDescriptor,
)
from .defaults import build_context_provider, default_context_plan_configuration
from .plan_configuration import ContextOwnerPlanConfiguration, InMemoryContextPlanConfiguration
from .ports import ContextDependencies, ContextPlanConfigurationPort
from .plan_resolver import ContextPlanResolver
from .provider import ContextProvider
from .registry import ContextSlotRegistry
from .signals import ContextRefreshReason, ContextRefreshSignal, ContextSignalBus, ContextSignalSubscription
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

__all__ = [
    "AvailableAgentsSlot",
    "ContextCacheScope",
    "ContextContribution",
    "ContextDependencies",
    "ContextOwnerPlanConfiguration",
    "ContextPlanConfigurationPort",
    "ContextMetadata",
    "ContextOwnerSnapshot",
    "ContextPlan",
    "ContextPlanResolver",
    "ContextPort",
    "ContextProvider",
    "ContextRefreshPolicy",
    "ContextRefreshReason",
    "ContextRefreshSignal",
    "ContextSignalBus",
    "ContextSignalSubscription",
    "ContextSlot",
    "ContextSlotBinding",
    "ContextSlotDescriptor",
    "ContextSlotManager",
    "ContextSlotRegistry",
    "InMemoryContextPlanConfiguration",
    "HistorySlot",
    "IdentitySlot",
    "KnowledgeSlot",
    "MemorySlot",
    "RunMessagesSlot",
    "SkillsSlot",
    "ToolsSlot",
    "UserInfoSlot",
    "build_context_provider",
    "default_context_plan_configuration",
]
