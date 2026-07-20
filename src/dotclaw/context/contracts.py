"""多 Owner Context Slot 的结构化公开契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from dotclaw.runtime.domain.context import ContextContributionKind, ContextOwner, ContextSlotStatus
from dotclaw.runtime.domain.facts import JSONMap


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
    content: str = ""
    message_ids: tuple[str, ...] = ()
    attributes: JSONMap = field(default_factory=dict)
    error_code: str = ""


@dataclass(frozen=True)
class ContextSlotDescriptor:
    """可注册 Slot 的静态声明。"""
    slot_id: str
    owner: ContextOwner
    contribution_kind: ContextContributionKind
    cache_scope: ContextCacheScope
    refresh_policy: ContextRefreshPolicy
    order: int


@dataclass(frozen=True)
class ContextSlotBinding:
    """某次 Context Plan 对一个已启用 Slot 的绑定。"""
    descriptor: ContextSlotDescriptor
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
    async def release(self) -> None:
        """释放 Slot 私有资源。"""
