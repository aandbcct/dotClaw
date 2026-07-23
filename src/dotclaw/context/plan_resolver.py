"""由 Owner 启用配置解析一次 Context Plan。"""

from __future__ import annotations

from dotclaw.agent.identity import AgentIdentity
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.domain.context import ContextOwner
from .contracts import ContextOwnerSnapshot, ContextPlan, ContextSlotBinding
from .plan_configuration import ContextOwnerPlanConfiguration, InMemoryContextPlanConfiguration
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


def build_context_plan_from_registry(registry: AgentRegistry) -> InMemoryContextPlanConfiguration:
    """基于完整 AgentRegistry 构造 Context Plan 配置（开发计划阶段4 修改项1）。

    保留默认 Owner Slot 配置，并将每个显式声明 ``context_slot_ids`` 的 Identity
    覆盖到对应 Agent Owner 的精确标识；未声明的 Identity 回退到默认计划。
    """
    from .defaults import default_context_plan_configuration  # 延迟导入避免与 defaults 的循环依赖

    defaults: InMemoryContextPlanConfiguration = default_context_plan_configuration()
    agent_overrides: dict[str, tuple[str, ...]] = {
        identity.agent_id: identity.context_slot_ids
        for identity in registry.list_all()
        if identity.context_slot_ids is not None
    }
    if not agent_overrides:
        return defaults
    return InMemoryContextPlanConfiguration(
        default_configurations=defaults.default_configurations,
        owner_configurations={ContextOwner.AGENT: agent_overrides},
    )
