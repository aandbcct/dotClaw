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
from .defaults import DEFAULT_CONTEXT_SLOT_IDS, build_context_provider
from .ports import ContextDependencies
from .plan_resolver import ContextPlanResolver
from .provider import ContextProvider
from .registry import ContextSlotRegistry
from .signals import ContextRefreshSignal, ContextSignalBus, ContextSignalSubscription
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
    "ContextMetadata",
    "ContextOwnerSnapshot",
    "ContextPlan",
    "ContextPlanResolver",
    "ContextPort",
    "ContextProvider",
    "ContextRefreshPolicy",
    "ContextRefreshSignal",
    "ContextSignalBus",
    "ContextSignalSubscription",
    "ContextSlot",
    "ContextSlotBinding",
    "ContextSlotDescriptor",
    "ContextSlotManager",
    "ContextSlotRegistry",
    "DEFAULT_CONTEXT_SLOT_IDS",
    "HistorySlot",
    "IdentitySlot",
    "KnowledgeSlot",
    "MemorySlot",
    "RunMessagesSlot",
    "SkillsSlot",
    "ToolsSlot",
    "UserInfoSlot",
    "build_context_provider",
]
