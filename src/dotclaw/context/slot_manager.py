"""Context Slot 实例缓存、刷新与按 Owner 生命周期释放。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import ContextOwner, ContextSlotStatus

from .contracts import ContextCacheScope, ContextContribution, ContextPlan, ContextSlot, ContextSlotBinding
from .registry import ContextSlotRegistry
from .signals import ContextRefreshSignal, ContextSignalBus


class ContextSlotManager:
    """管理 Slot 私有实例，不读取或写入任何 Owner 领域数据。"""
    def __init__(self, registry: ContextSlotRegistry, signal_bus: ContextSignalBus) -> None:
        self._registry: ContextSlotRegistry = registry
        self._signal_bus: ContextSignalBus = signal_bus
        self._instances: dict[tuple[str, ContextOwner, str], ContextSlot] = {}
        self._bindings: dict[tuple[str, ContextOwner, str], ContextSlotBinding] = {}
        self._invalid_bindings: set[tuple[str, ContextOwner, str]] = set()
    async def load_plan(self, plan: ContextPlan) -> tuple[ContextContribution, ...]:
        """在安全点处理信号后加载所有已绑定 Slot。"""
        loaded_slots: list[tuple[ContextSlotBinding, ContextSlot]] = []
        binding: ContextSlotBinding
        for binding in plan.bindings:
            self._signal_bus.subscribe(binding.descriptor.slot_id, binding.descriptor.owner, binding.owner_key)
            slot: ContextSlot = self._slot(binding)
            self._bindings[_binding_key(binding)] = binding
            loaded_slots.append((binding, slot))
        await self.drain_signals()
        results: list[ContextContribution] = []
        slot: ContextSlot
        for binding, slot in loaded_slots:
            binding_key: tuple[str, ContextOwner, str] = _binding_key(binding)
            try:
                if binding_key in self._invalid_bindings:
                    await slot.refresh(binding)
                results.append(await slot.load(binding))
            except Exception as error:
                results.append(ContextContribution(
                    binding.descriptor.contribution_kind,
                    status=ContextSlotStatus.FAILED,
                    error_code=type(error).__name__,
                ))
            finally:
                self._invalid_bindings.discard(binding_key)
        return tuple(results)
    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """标记精确 Owner 的 Slot 实例在下一安全点刷新。"""
        self._invalid_bindings.add((slot_id, owner, owner_key))
    async def drain_signals(self) -> None:
        """将定向事件交给已订阅 Slot 根据事件载荷决定是否失效。"""
        for signal in self._signal_bus.drain():
            binding_key: tuple[str, ContextOwner, str] = (
                signal.slot_id,
                signal.owner,
                signal.owner_key,
            )
            binding: ContextSlotBinding | None = self._bindings.get(binding_key)
            slot: ContextSlot | None = self._instances.get(binding_key)
            if binding is not None and slot is not None and slot.should_refresh(binding, signal):
                self._invalid_bindings.add(binding_key)
    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """在 Owner 终态释放对应生命周期的 Slot 实例。"""
        scopes: dict[ContextOwner, ContextCacheScope] = {
            ContextOwner.AGENT: ContextCacheScope.AGENT,
            ContextOwner.SESSION: ContextCacheScope.SESSION,
            ContextOwner.RUN: ContextCacheScope.RUN,
            ContextOwner.GLOBAL: ContextCacheScope.NONE,
        }
        cache_key: tuple[str, ContextOwner, str]
        for cache_key in tuple(self._instances):
            slot_id, cached_owner, cached_owner_key = cache_key
            if (
                cached_owner is owner
                and cached_owner_key == owner_key
                and self._registry.descriptor(slot_id).cache_scope is scopes[owner]
            ):
                await self._instances.pop(cache_key).release()
                self._bindings.pop(cache_key, None)
                self._invalid_bindings.discard(cache_key)
    def _slot(self, binding: ContextSlotBinding) -> ContextSlot:
        """按缓存范围获得 Slot 实例。"""
        slot_id: str = binding.descriptor.slot_id
        if binding.descriptor.cache_scope is ContextCacheScope.NONE:
            return self._registry.create(slot_id)
        cache_key: tuple[str, ContextOwner, str] = _binding_key(binding)
        if cache_key not in self._instances:
            self._instances[cache_key] = self._registry.create(slot_id)
        return self._instances[cache_key]


def _binding_key(binding: ContextSlotBinding) -> tuple[str, ContextOwner, str]:
    """以 Slot、Owner 类型和精确 Owner 标识隔离实例及失效状态。"""
    return binding.descriptor.slot_id, binding.descriptor.owner, binding.owner_key
