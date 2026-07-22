"""tools.registry 的重名拒绝与不可变快照测试（阶段一）。"""

from __future__ import annotations

import pytest

from dotclaw.tools.base import ToolDefinition, ToolResult, ToolSource
from dotclaw.tools.handler import ToolHandler
from dotclaw.tools.registry import DuplicateToolError, ToolRegistry


class _StubHandler(ToolHandler):
    """最小 ToolHandler 替身，用于注册表行为测试。"""

    def __init__(self, name: str, source: ToolSource = ToolSource.BUILTIN) -> None:
        self._def = ToolDefinition(name=name, description="", source=source)

    def definition(self) -> ToolDefinition:
        return self._def

    async def execute(self, arguments, context=None) -> ToolResult:
        return ToolResult()


def test_register_and_query() -> None:
    """注册后可查询，未注册返回 None。"""
    reg = ToolRegistry()
    reg.register(_StubHandler("a"))
    assert reg.get("a") is not None
    assert reg.get("missing") is None


def test_duplicate_registration_fails_with_both_sources() -> None:
    """同名注册必须失败，且异常包含冲突双方的名称与来源。"""
    reg = ToolRegistry()
    reg.register(_StubHandler("dup", ToolSource.BUILTIN))
    with pytest.raises(DuplicateToolError) as exc:
        reg.register(_StubHandler("dup", ToolSource.MCP))
    assert exc.value.name == "dup"
    assert exc.value.existing_source == "builtin"
    assert exc.value.new_source == "mcp"
    assert "dup" in str(exc.value)


def test_snapshot_is_immutable_to_later_registration() -> None:
    """快照不随后续注册而变化。"""
    reg = ToolRegistry()
    reg.register(_StubHandler("a"))
    snap1 = reg.snapshot()
    reg.register(_StubHandler("b"))
    snap2 = reg.snapshot()
    assert len(snap1) == 1
    assert len(snap2) == 2


def test_snapshot_is_deep_copy_not_affected_by_mutation() -> None:
    """修改原 Handler 的 definition 不影响已取出的快照（深拷贝）。"""
    reg = ToolRegistry()
    reg.register(_StubHandler("a"))
    snap = reg.snapshot()
    reg.get("a").definition().name = "mutated"
    assert snap[0].name == "a"


def test_get_definitions_returns_list_based_on_snapshot() -> None:
    """get_definitions 返回基于快照的列表，旧列表不被后续修改影响。"""
    reg = ToolRegistry()
    reg.register(_StubHandler("a"))
    defs = reg.get_definitions()
    assert isinstance(defs, list)
    assert defs[0].name == "a"
    reg.register(_StubHandler("b"))
    assert len(defs) == 1


def test_unregister_allows_reregister() -> None:
    """注销后同名可重新注册，不触发冲突。"""
    reg = ToolRegistry()
    reg.register(_StubHandler("a"))
    assert reg.unregister("a") is True
    reg.register(_StubHandler("a"))
    assert reg.get("a") is not None
