"""测试具体 Slot 实现"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dotclaw.agent.slotContext import (
    SlotContext,
    ContextAssembler,
    TierLevel,
)
from src.dotclaw.agent.slotContextImp import (
    IdentitySlot,
    ToolsSlot,
    SkillsSlot,
    WorkspaceSlot,
    UserInfoSlot,
    MemorySlot,
    KnowledgeSlot,
    ProjectSlot,
)
from src.dotclaw.memory.storage import SearchResult


def _make_ctx(**overrides) -> SlotContext:
    """构造测试用 SlotContext"""
    defaults = {
        "query": "test query",
        "request_id": "req-001",
        "session_id": "sess-001",
        "project_root": Path("/fake"),
        "max_context_tokens": 8000,
    }
    defaults.update(overrides)
    return SlotContext(**defaults)


# ======================== IdentitySlot ========================

class TestIdentitySlot:
    @pytest.mark.asyncio
    async def test_returns_system_prompt(self) -> None:
        slot = IdentitySlot()
        ctx = _make_ctx(system_prompt="你是 dotClaw，一个 AI 助手。")
        result = await slot.load(ctx)
        assert result == "你是 dotClaw，一个 AI 助手。"

    @pytest.mark.asyncio
    async def test_tier_is_static(self) -> None:
        assert IdentitySlot.tier == TierLevel.STATIC

    @pytest.mark.asyncio
    async def test_cache_policy_is_forever(self) -> None:
        assert IdentitySlot.cache_policy == "forever"


# ======================== ToolsSlot ========================

class TestToolsSlot:
    @pytest.mark.asyncio
    async def test_formats_tools(self) -> None:
        slot = ToolsSlot()
        ctx = _make_ctx(tool_definitions=[
            MagicMock(name="read_file", description="读取文件", parameters={}),
            MagicMock(name="write_file", description="写入文件", parameters={}),
        ])
        result = await slot.load(ctx)
        assert result is not None
        assert "read_file" in result
        assert "write_file" in result
        assert "读取文件" in result

    @pytest.mark.asyncio
    async def test_tier_is_session(self) -> None:
        assert ToolsSlot.tier == TierLevel.SESSION


# ======================== SkillsSlot ========================

class TestSkillsSlot:
    @pytest.mark.asyncio
    async def test_no_registry_returns_none(self) -> None:
        slot = SkillsSlot()
        ctx = _make_ctx(skill_registry=None)
        result = await slot.load(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_with_registry(self) -> None:
        slot = SkillsSlot()
        registry = MagicMock()
        registry.get_descriptions_block.return_value = "- skill_a: desc a\n- skill_b: desc b"
        ctx = _make_ctx(skill_registry=registry)
        result = await slot.load(ctx)
        assert result is not None
        assert "技能系统" in result
        assert "skill_a" in result


# ======================== WorkspaceSlot ========================

class TestWorkspaceSlot:
    @pytest.mark.asyncio
    async def test_returns_workspace(self) -> None:
        slot = WorkspaceSlot()
        ctx = _make_ctx(project_root=Path("/fake/project"))
        result = await slot.load(ctx)
        assert result is not None
        assert "工作空间" in result
        assert "fake" in result
        assert "project" in result

    @pytest.mark.asyncio
    async def test_tier_is_session(self) -> None:
        assert WorkspaceSlot.tier == TierLevel.SESSION


# ======================== UserInfoSlot ========================

class TestUserInfoSlot:
    @pytest.mark.asyncio
    async def test_no_profile_returns_none(self) -> None:
        slot = UserInfoSlot()
        ctx = _make_ctx(user_profile=None)
        result = await slot.load(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_with_name(self) -> None:
        slot = UserInfoSlot()
        profile = MagicMock()
        profile.name = "atozn"
        profile.preferred_language = "中文"
        ctx = _make_ctx(user_profile=profile)
        result = await slot.load(ctx)
        assert result is not None
        assert "atozn" in result


# ======================== MemorySlot ========================

class TestMemorySlot:
    @pytest.mark.asyncio
    async def test_no_manager_returns_none(self) -> None:
        slot = MemorySlot()
        ctx = _make_ctx(memory_manager=None)
        result = await slot.load(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_search_result(self) -> None:
        slot = MemorySlot()
        mgr = MagicMock()
        mgr.search = AsyncMock(return_value=[
            SearchResult(
                path="memory/context.md",
                start_line=1,
                end_line=1,
                score=0.9,
                snippet="上次我们讨论了上下文模块",
                source="memory",
                title="上下文",
            )
        ])
        ctx = _make_ctx(memory_manager=mgr, query="上下文模块")
        result = await slot.load(ctx)
        assert result is not None
        assert "相关记忆" in result
        assert "上下文模块" in result

    @pytest.mark.asyncio
    async def test_tier_is_conditional(self) -> None:
        assert MemorySlot.tier == TierLevel.CONDITIONAL

    @pytest.mark.asyncio
    async def test_cache_policy_is_request(self) -> None:
        assert MemorySlot.cache_policy == "request"


# ======================== KnowledgeSlot ========================

class TestKnowledgeSlot:
    @pytest.mark.asyncio
    async def test_no_base_returns_none(self) -> None:
        slot = KnowledgeSlot()
        ctx = _make_ctx(knowledge_base=None)
        result = await slot.load(ctx)
        assert result is None


# ======================== ProjectSlot ========================

class TestProjectSlot:
    @pytest.mark.asyncio
    async def test_no_files_returns_none(self) -> None:
        slot = ProjectSlot()
        ctx = _make_ctx(project_root=Path("/nonexistent"))
        result = await slot.load(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_tier_is_conditional(self) -> None:
        assert ProjectSlot.tier == TierLevel.CONDITIONAL

    @pytest.mark.asyncio
    async def test_cache_policy_is_request(self) -> None:
        assert ProjectSlot.cache_policy == "request"


# ======================== Assembler 集成 ========================

class TestAssemblerIntegration:
    """验证 Assembler + 真实 Slot 的综合行为"""

    @pytest.mark.asyncio
    async def test_full_assembly(self) -> None:
        identity = IdentitySlot()
        identity._cached = "你是 dotClaw。"  # 跳过 _produce
        identity._cache_valid = True

        tools = ToolsSlot()
        tools._cached = "## 可用工具\n\n- read_file\n- write_file"
        tools._cache_valid = True

        workspace = WorkspaceSlot()
        workspace._cached = "工作空间: /fake/project"
        workspace._cache_valid = True

        memory = MemorySlot()
        memory._cached = None  # 无记忆
        memory._cache_valid = True

        assembler = ContextAssembler([identity, workspace, tools, memory])
        ctx = _make_ctx()
        result = await assembler.build_system_prompt(ctx)

        assert "你是 dotClaw。" in result
        assert "工作空间" in result
        assert "可用工具" in result
