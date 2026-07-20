"""由 Owner 启用配置解析一次 Context Plan。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import ContextOwner
from .contracts import ContextOwnerSnapshot, ContextPlan, ContextSlotBinding
from .registry import ContextSlotRegistry


class ContextPlanResolver:
    """只解析绑定与排序，不加载 Slot 内容。"""
    def __init__(self, registry: ContextSlotRegistry) -> None:
        self._registry: ContextSlotRegistry = registry
    def resolve(
        self,
        enabled_slot_ids: tuple[str, ...],
        owner_snapshots: dict[ContextOwner, ContextOwnerSnapshot],
    ) -> ContextPlan:
        """按 Descriptor 顺序生成本次有效绑定。"""
        bindings: list[ContextSlotBinding] = []
        slot_id: str
        for slot_id in enabled_slot_ids:
            descriptor = self._registry.descriptor(slot_id)
            owner_snapshot: ContextOwnerSnapshot = owner_snapshots.get(
                descriptor.owner,
                ContextOwnerSnapshot("", {}),
            )
            bindings.append(ContextSlotBinding(descriptor, owner_snapshot.owner_key, owner_snapshot.data))
        return ContextPlan(tuple(sorted(bindings, key=lambda binding: binding.descriptor.order)))
