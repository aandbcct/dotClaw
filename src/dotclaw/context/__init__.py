"""Runtime v2 的上下文构建实现。"""

from .ports import ContextDependencies, ContextMetadata, ContextPort
from .scoped_cache import ScopedCache, SlotCacheScope
from .slot_context import ContextProfile, SlotContext
from .slot_context_provider import LegacyContextPortAdapter, SlotContextProvider
from .slots import (
    AvailableAgentsSlot,
    IdentitySlot,
    KnowledgeSlot,
    MemorySlot,
    ProjectSlot,
    SkillsSlot,
    ToolsSlot,
    UserInfoSlot,
    WorkspaceSlot,
)

__all__ = [
    "AvailableAgentsSlot",
    "ContextDependencies",
    "ContextMetadata",
    "ContextPort",
    "ContextProfile",
    "IdentitySlot",
    "KnowledgeSlot",
    "LegacyContextPortAdapter",
    "MemorySlot",
    "ProjectSlot",
    "ScopedCache",
    "SkillsSlot",
    "SlotCacheScope",
    "SlotContext",
    "SlotContextProvider",
    "ToolsSlot",
    "UserInfoSlot",
    "WorkspaceSlot",
]
