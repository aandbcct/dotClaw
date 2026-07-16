"""SlotContext 与 ContextAssembler 的公开契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.agent.slotContext import ContextAssembler, ContextSlot, SlotContext, TierLevel


def _context() -> SlotContext:
    """构造最小上下文输入。"""
    return SlotContext(
        query="测试",
        request_id="request-1",
        session_id="session-1",
        project_root=Path("."),
        max_context_tokens=1000,
    )


def test_slot_context_is_immutable() -> None:
    """组装输入是不可变快照，避免 Slot 间隐式修改。"""
    context = _context()

    with pytest.raises(Exception):
        context.query = "被修改"  # type: ignore[misc]


class FailingSlot(ContextSlot):
    """模拟不可用的可选上下文来源。"""

    name = "failing"
    tier = TierLevel.SESSION
    cache_policy = "session"

    async def _produce(self, ctx: SlotContext) -> str | None:
        raise RuntimeError("来源不可用")


class StaticSlot(ContextSlot):
    """返回稳定内容的上下文来源。"""

    name = "static"
    tier = TierLevel.STATIC
    cache_policy = "forever"

    async def _produce(self, ctx: SlotContext) -> str | None:
        return "保留内容"


@pytest.mark.asyncio
async def test_assembler_skips_failing_slot_and_keeps_other_content() -> None:
    """单个 Slot 失败不能阻断其他上下文的组装。"""
    assembler = ContextAssembler([FailingSlot(), StaticSlot()])

    prompt = await assembler.build_system_prompt(_context())

    assert prompt == "保留内容"
