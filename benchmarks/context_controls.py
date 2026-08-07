"""PR6 Benchmark 对照控制：观察 Provider 加载，强制重建不进入生产 Runtime。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class ObservedExternalSlotProvider:
    """记录型外部 Slot Provider（外部上下文载入替身）。"""

    value: str
    delay_ms: int = 0
    load_count: int = 0

    async def load(self) -> str:
        """返回当前值并记录一次外部载入。"""
        self.load_count += 1
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        return self.value


@dataclass(frozen=True)
class ReplayControl:
    """回放或强制重建的 Benchmark 控制开关，不暴露给生产 ContextProvider。"""

    force_rebuild: bool = False

    async def materialize(self, persisted_snapshot: str, provider: ObservedExternalSlotProvider) -> str:
        """正常回放直接使用持久化快照；对照模式才读取 Provider。"""
        if not self.force_rebuild:
            return persisted_snapshot
        return await provider.load()
