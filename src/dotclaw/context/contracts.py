"""多 Owner Context Slot 的结构化公开契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextPersistenceMode,
    ContextSlotContent,
    ContextSlotStatus,
    TextSlotContent,
)
from dotclaw.runtime.domain.facts import JSONMap

from .signals import ContextRefreshSignal


class ContextCacheScope(StrEnum):
    """Slot 实例缓存的生命周期范围。"""
    AGENT = "agent"
    SESSION = "session"
    RUN = "run"
    NONE = "none"


class ContextRefreshPolicy(StrEnum):
    """Slot 内容刷新的触发策略。"""
    ON_DEMAND = "on_demand"
    SIGNAL = "signal"


@dataclass(frozen=True)
class ContextContribution:
    """Slot 对本次模型上下文的结构化贡献。"""
    kind: ContextContributionKind
    status: ContextSlotStatus
    content: ContextSlotContent = TextSlotContent("")
    error_code: str = ""



@dataclass(frozen=True)
class ContextSlotDescriptor:
    """可注册 Slot 的静态声明。"""
    slot_id: str
    owner: ContextOwner
    contribution_kind: ContextContributionKind
    persistence_mode: ContextPersistenceMode
    cache_scope: ContextCacheScope
    refresh_policy: ContextRefreshPolicy
    order: int


@dataclass(frozen=True)
class ContextOwnerSnapshot:
    """某个 Owner 在一次 Plan 解析时提供的标识与只读数据。"""

    owner_key: str
    data: JSONMap


@dataclass(frozen=True)
class ContextSlotBinding:
    """某次 Context Plan 对一个已启用 Slot 的绑定。"""
    descriptor: ContextSlotDescriptor
    owner_key: str
    owner_data: JSONMap


@dataclass(frozen=True)
class ContextPlan:
    """一次调用有效、排序后的 Slot 绑定集合。"""
    bindings: tuple[ContextSlotBinding, ...]


class ContextSlot(Protocol):
    """不拥有领域数据的结构化上下文加载接口。"""
    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """从绑定的 Owner 快照读取结构化贡献。"""
    async def refresh(self, binding: ContextSlotBinding) -> None:
        """失效当前绑定的私有缓存。"""
    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """读取完整刷新事件并决定当前绑定是否需要失效。"""
    async def release(self) -> None:
        """释放 Slot 私有资源。"""
