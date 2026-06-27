"""测试 ContextSlot 基类 + TierLevel 枚举 + SlotContext 数据类"""

from __future__ import annotations

import pytest
from pathlib import Path

from src.dotclaw.agent.slotContext import (
    TierLevel,
    SlotContext,
    ContextSlot,
    ContextAssembler,
)


# ======================== TierLevel ========================

class TestTierLevel:
    """测试 TierLevel 枚举的排序和插值能力"""

    def test_int_values(self) -> None:
        assert TierLevel.STATIC == 0
        assert TierLevel.SESSION == 10
        assert TierLevel.CONDITIONAL == 20
        assert TierLevel.DYNAMIC == 30

    def test_ordering(self) -> None:
        """数值越大越靠后"""
        levels = sorted(TierLevel, key=lambda x: x.value)
        assert levels == [
            TierLevel.STATIC,
            TierLevel.SESSION,
            TierLevel.CONDITIONAL,
            TierLevel.DYNAMIC,
        ]

    def test_gap_allows_insertion(self) -> None:
        """间隔 10 允许在中间插入新层级"""
        assert TierLevel.SESSION.value - TierLevel.STATIC.value == 10
        assert TierLevel.CONDITIONAL.value - TierLevel.SESSION.value == 10


# ======================== ContextSlot ========================

class _TestSlot(ContextSlot):
    """测试用 Slot：记录 _produce 调用次数"""
    name = "test"
    tier = TierLevel.STATIC
    cache_policy = "forever"

    def __init__(self) -> None:
        super().__init__()
        self._produce_count = 0

    async def _produce(self, ctx: SlotContext) -> str | None:
        self._produce_count += 1
        return f"produced_{self._produce_count}"


class TestContextSlotCaching:
    """测试 ContextSlot 缓存机制"""

    @pytest.fixture
    def ctx(self) -> SlotContext:
        return SlotContext(
            query="test query",
            request_id="req-001",
            agent_config=None,
            session_id="sess-001",
            project_root=Path("/fake"),
            max_context_tokens=8000,
            tool_definitions=[],
            skill_registry=None,
            memory_manager=None,
            knowledge_base=None,
            user_profile=None,
            journal=None,
        )

    @pytest.mark.asyncio
    async def test_first_load_calls_produce(self, ctx: SlotContext) -> None:
        slot = _TestSlot()
        assert slot._produce_count == 0
        result = await slot.load(ctx)
        assert result == "produced_1"
        assert slot._produce_count == 1

    @pytest.mark.asyncio
    async def test_second_load_uses_cache(self, ctx: SlotContext) -> None:
        slot = _TestSlot()
        await slot.load(ctx)  # first
        result = await slot.load(ctx)  # second → cached
        assert result == "produced_1"
        assert slot._produce_count == 1  # NOT incremented

    @pytest.mark.asyncio
    async def test_invalidate_triggers_reproduce(self, ctx: SlotContext) -> None:
        slot = _TestSlot()
        await slot.load(ctx)
        slot.invalidate()
        result = await slot.load(ctx)
        assert result == "produced_2"
        assert slot._produce_count == 2

    @pytest.mark.asyncio
    async def test_produce_returns_none_skips(self, ctx: SlotContext) -> None:
        """_produce 返回 None 的情况"""
        empty_slot = _EmptySlot()
        result = await empty_slot.load(ctx)
        assert result is None


class _EmptySlot(ContextSlot):
    name = "empty"
    tier = TierLevel.CONDITIONAL
    cache_policy = "request"

    async def _produce(self, ctx: SlotContext) -> str | None:
        return None


# ======================== ContextAssembler ========================

class TestContextAssembler:
    """测试 ContextAssembler"""

    @pytest.fixture
    def ctx(self) -> SlotContext:
        return SlotContext(
            query="test",
            request_id="r1",
            agent_config=None,
            session_id="s1",
            project_root=Path("/fake"),
            max_context_tokens=8000,
            tool_definitions=[],
            skill_registry=None,
            memory_manager=None,
            knowledge_base=None,
            user_profile=None,
            journal=None,
        )

    @pytest.mark.asyncio
    async def test_sorts_slots_by_tier(self, ctx: SlotContext) -> None:
        """Assembler 按 tier 排序输出"""
        a_slot = _StaticSlot("a", TierLevel.STATIC)
        b_slot = _StaticSlot("b", TierLevel.SESSION)
        c_slot = _StaticSlot("c", TierLevel.DYNAMIC)

        assembler = ContextAssembler([c_slot, a_slot, b_slot])
        result = await assembler.build_system_prompt(ctx)
        # a (0), b (10), c (30)
        assert result == "content_a\n\ncontent_b\n\ncontent_c"

    @pytest.mark.asyncio
    async def test_skips_none_content(self, ctx: SlotContext) -> None:
        """返回 None 的 Slot 被跳过"""
        on_slot = _StaticSlot("on", TierLevel.STATIC)  # 有内容
        empty_slot = _EmptyContentSlot("empty", TierLevel.STATIC)  # None

        assembler = ContextAssembler([empty_slot, on_slot])
        result = await assembler.build_system_prompt(ctx)
        assert result == "content_on"

    @pytest.mark.asyncio
    async def test_on_new_request_invalidates_request_cache(self, ctx: SlotContext) -> None:
        """on_new_request 只使 request 缓存的 slot 过期"""
        forever_slot = _ProduceCountSlot("f", TierLevel.STATIC, "forever")
        session_slot = _ProduceCountSlot("s", TierLevel.SESSION, "session")
        request_slot = _ProduceCountSlot("r", TierLevel.CONDITIONAL, "request")

        assembler = ContextAssembler([forever_slot, session_slot, request_slot])

        await assembler.build_system_prompt(ctx)
        assert forever_slot.produce_count == 1
        assert session_slot.produce_count == 1
        assert request_slot.produce_count == 1

        # 同一 request 内再调 → 全缓存命中
        await assembler.build_system_prompt(ctx)
        assert forever_slot.produce_count == 1
        assert session_slot.produce_count == 1
        assert request_slot.produce_count == 1

        # 新 request → tier 2 过期
        assembler.on_new_request()
        await assembler.build_system_prompt(ctx)
        assert forever_slot.produce_count == 1  # forever: still cached
        assert session_slot.produce_count == 1  # session: still cached
        assert request_slot.produce_count == 2  # request: re-produced


class _StaticSlot(ContextSlot):
    """固定返回 `content_{name}` 的 Slot"""
    def __init__(self, name: str, tier: TierLevel) -> None:
        super().__init__()
        self.name = name
        self.tier = tier
        self.cache_policy = "forever"

    async def _produce(self, ctx: SlotContext) -> str | None:
        return f"content_{self.name}"


class _EmptyContentSlot(ContextSlot):
    """始终返回 None 的 Slot"""
    def __init__(self, name: str, tier: TierLevel) -> None:
        super().__init__()
        self.name = name
        self.tier = tier
        self.cache_policy = "forever"

    async def _produce(self, ctx: SlotContext) -> str | None:
        return None


class _ProduceCountSlot(ContextSlot):
    """记录 _produce 调用次数的 Slot"""
    def __init__(self, name: str, tier: TierLevel, cache_policy: str) -> None:
        super().__init__()
        self.name = name
        self.tier = tier
        self.cache_policy = cache_policy
        self.produce_count = 0

    async def _produce(self, ctx: SlotContext) -> str | None:
        self.produce_count += 1
        return f"content_{self.name}_{self.produce_count}"
