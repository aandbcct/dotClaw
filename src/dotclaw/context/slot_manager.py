"""Context Slot 实例缓存、刷新与按 Owner 生命周期释放。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import ContextOwner, ContextSlotStatus

from .contracts import ContextCacheScope, ContextContribution, ContextPlan, ContextSlot, ContextSlotBinding
from .registry import ContextSlotRegistry
from .signals import ContextSignalBus


class ContextSlotManager:
    """管理 Slot 私有实例，不读取或写入任何 Owner 领域数据。"""
    def __init__(self, registry: ContextSlotRegistry, signal_bus: ContextSignalBus) -> None:
        self._registry: ContextSlotRegistry = registry
        self._signal_bus: ContextSignalBus = signal_bus
        self._instances: dict[tuple[str, str], ContextSlot] = {}
        self._invalid_slots: set[str] = set()
    async def load_plan(self, plan: ContextPlan) -> tuple[ContextContribution, ...]:
        """在安全点处理信号后加载所有已绑定 Slot。"""
        await self.drain_signals()
        results: list[ContextContribution] = []
        binding: ContextSlotBinding
        for binding in plan.bindings:
            self._signal_bus.subscribe(binding.descriptor.slot_id)
            slot: ContextSlot = self._slot(binding)
            try:
                if binding.descriptor.slot_id in self._invalid_slots:
                    await slot.refresh(binding)
                results.append(await slot.load(binding))
            except Exception as error:
                results.append(ContextContribution(
                    binding.descriptor.contribution_kind,
                    status=ContextSlotStatus.FAILED,
                    error_code=type(error).__name__,
                ))
        self._invalid_slots.clear()
        return tuple(results)
    def request_refresh(self, slot_id: str) -> None:
        """标记 Slot 在下一安全点刷新。"""
        self._invalid_slots.add(slot_id)
    async def drain_signals(self) -> None:
        """消费 SignalBus 信号，不向外泄露具体 Slot 实例。"""
        for signal in self._signal_bus.drain():
            self.request_refresh(signal.slot_id)
    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """在 Owner 终态释放对应生命周期的 Slot 实例。"""
        scopes: dict[ContextOwner, ContextCacheScope] = {
            ContextOwner.AGENT: ContextCacheScope.AGENT,
            ContextOwner.SESSION: ContextCacheScope.SESSION,
            ContextOwner.RUN: ContextCacheScope.RUN,
            ContextOwner.GLOBAL: ContextCacheScope.NONE,
        }
        cache_key: tuple[str, str]
        for cache_key in tuple(self._instances):
            slot_id, cached_owner_key = cache_key
            if cached_owner_key == owner_key and self._registry.descriptor(slot_id).cache_scope is scopes[owner]:
                await self._instances.pop(cache_key).release()
    def _slot(self, binding: ContextSlotBinding) -> ContextSlot:
        """按缓存范围获得 Slot 实例。"""
        slot_id: str = binding.descriptor.slot_id
        if binding.descriptor.cache_scope is ContextCacheScope.NONE:
            return self._registry.create(slot_id)
        cache_key: tuple[str, str] = _cache_key(binding)
        if cache_key not in self._instances:
            self._instances[cache_key] = self._registry.create(slot_id)
        return self._instances[cache_key]


def _cache_key(binding: ContextSlotBinding) -> tuple[str, str]:
    """以 Slot 和精确 Owner 标识隔离同类实例缓存。"""
    return binding.descriptor.slot_id, binding.owner_key
