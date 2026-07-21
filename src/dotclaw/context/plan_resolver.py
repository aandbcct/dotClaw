"""由 Owner 启用配置解析一次 Context Plan。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import ContextOwner
from .contracts import ContextOwnerSnapshot, ContextPlan, ContextSlotBinding
from .ports import ContextPlanConfigurationPort
from .registry import ContextSlotRegistry


class ContextPlanResolver:
    """只解析绑定与排序，不加载 Slot 内容。"""
    def __init__(
        self,
        registry: ContextSlotRegistry,
        plan_configuration: ContextPlanConfigurationPort,
    ) -> None:
        self._registry: ContextSlotRegistry = registry
        self._plan_configuration: ContextPlanConfigurationPort = plan_configuration
    def resolve(
        self,
        owner_snapshots: dict[ContextOwner, ContextOwnerSnapshot],
    ) -> ContextPlan:
        """按各 Owner 的有效配置和 Descriptor 顺序生成本次绑定。"""
        bindings: list[ContextSlotBinding] = []
        owner: ContextOwner
        owner_snapshot: ContextOwnerSnapshot
        slot_id: str
        for owner, owner_snapshot in owner_snapshots.items():
            for slot_id in self._plan_configuration.enabled_slot_ids(owner, owner_snapshot.owner_key):
                descriptor = self._registry.descriptor(slot_id)
                if descriptor.owner is not owner:
                    raise ValueError(f"Context Slot {slot_id} 的 Owner 与启用配置不一致")
                bindings.append(ContextSlotBinding(descriptor, owner_snapshot.owner_key, owner_snapshot.data))
        return ContextPlan(tuple(sorted(bindings, key=lambda binding: binding.descriptor.order)))
