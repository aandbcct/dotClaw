"""按 Owner 配置有效 Context Slot 的内存适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from dotclaw.runtime.domain.context import ContextOwner


@dataclass(frozen=True)
class ContextOwnerPlanConfiguration:
    """一个 Owner 类型的默认 Slot 启用配置 DTO。"""

    owner: ContextOwner
    slot_ids: tuple[str, ...]


@dataclass(frozen=True)
class InMemoryContextPlanConfiguration:
    """按 Global、Agent、Session、Run Owner 解析 Slot 的内存适配器。"""

    default_configurations: tuple[ContextOwnerPlanConfiguration, ...] = ()
    owner_configurations: Mapping[ContextOwner, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    def enabled_slot_ids(self, owner: ContextOwner, owner_key: str) -> tuple[str, ...]:
        """优先返回精确 Owner Key 配置，缺失时回退到该 Owner 的默认配置。"""
        configured_by_key: Mapping[str, tuple[str, ...]] | None = self.owner_configurations.get(owner)
        if configured_by_key is not None and owner_key in configured_by_key:
            return configured_by_key[owner_key]
        configuration: ContextOwnerPlanConfiguration | None = next(
            (item for item in self.default_configurations if item.owner is owner),
            None,
        )
        return () if configuration is None else configuration.slot_ids
