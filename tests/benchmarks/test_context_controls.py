"""PR6 回放与强制重建控制测试。"""

import pytest

from benchmarks.context_controls import ObservedExternalSlotProvider, ReplayControl


@pytest.mark.asyncio
async def test_replay_does_not_reload_external_provider() -> None:
    """正常回放必须只使用持久化快照。"""
    provider = ObservedExternalSlotProvider("v2")
    assert await ReplayControl().materialize("v1", provider) == "v1"
    assert provider.load_count == 0


@pytest.mark.asyncio
async def test_forced_rebuild_is_benchmark_only_control() -> None:
    """强制重建控制才会读取外部来源。"""
    provider = ObservedExternalSlotProvider("v2")
    assert await ReplayControl(force_rebuild=True).materialize("v1", provider) == "v2"
    assert provider.load_count == 1
