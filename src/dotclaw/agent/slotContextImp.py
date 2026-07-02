"""具体 ContextSlot 实现

每个 Slot 负责从特定来源加载内容，以纯文本形式输出。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .slotContext import ContextSlot, SlotContext, TierLevel

if TYPE_CHECKING:
    from ..skills.registry import SkillRegistry
    from ..llm.base import ToolDefinition

logger = logging.getLogger("dotclaw.agent.context_slots")


# ======================== IdentitySlot (tier 0) ========================


class IdentitySlot(ContextSlot):
    """Agent 身份定义。从 SlotContext.system_prompt 读取。

    该值由 Agent._resolve_system_prompt() 预解析：
    agent_config.system_prompt_template > config.agent.system_prompt
    """

    name = "identity"
    tier = TierLevel.STATIC
    cache_policy = "forever"

    async def _produce(self, ctx: SlotContext) -> str | None:
        return ctx.system_prompt or None


# ======================== ToolsSlot (tier 1) ========================


class ToolsSlot(ContextSlot):
    """可用工具列表。从 SlotContext.tool_definitions 格式化。"""

    name = "tools"
    tier = TierLevel.SESSION
    cache_policy = "session"

    async def _produce(self, ctx: SlotContext) -> str | None:
        tools: list[Any] = ctx.tool_definitions
        if not tools:
            return None

        lines: list[str] = ["## 可用工具"]
        for tool in tools:
            name: str = getattr(tool, "name", str(tool))
            description: str = getattr(tool, "description", "")
            parameters: Any = getattr(tool, "parameters", None)

            lines.append(f"### {name}")
            if description:
                lines.append(description)
            if parameters:
                import json as _json
                params_str = _json.dumps(parameters, ensure_ascii=False, indent=2)
                lines.append(f"参数: {params_str}")
            lines.append("")
        return "\n".join(lines)


# ======================== SkillsSlot (tier 1) ========================


class SkillsSlot(ContextSlot):
    """可用技能列表。从 SlotContext.skill_registry 读取。"""

    name = "skills"
    tier = TierLevel.SESSION
    cache_policy = "session"

    async def _produce(self, ctx: SlotContext) -> str | None:
        registry: "SkillRegistry | None" = ctx.skill_registry
        if not registry:
            return None

        descriptions: str = registry.get_descriptions_block(max_desc_len=20)
        if not descriptions:
            return None

        return (
            "## 技能系统（mandatory）\n\n"
            "如果有技能的描述与用户需求匹配：使用 `read_file` 工具读取其路径的 SKILL.md 文件，\n"
            "然后严格遵循文件中的指令。\n\n"
            "**重要**: 技能不是工具，不能直接调用。使用技能的唯一方式是用 `read_file` 读取 SKILL.md 文件，\n"
            "然后按文件内容操作。\n\n"
            "### 可用技能\n\n"
            f"{descriptions}"
        )


# ======================== WorkspaceSlot (tier 1) ========================


class WorkspaceSlot(ContextSlot):
    """当前工作空间路径。"""

    name = "workspace"
    tier = TierLevel.SESSION
    cache_policy = "session"

    async def _produce(self, ctx: SlotContext) -> str | None:
        return f"工作空间: {ctx.project_root}"


# ======================== UserInfoSlot (tier 1) ========================


class UserInfoSlot(ContextSlot):
    """用户身份信息。从 SlotContext.user_profile 读取。"""

    name = "user_info"
    tier = TierLevel.SESSION
    cache_policy = "session"

    async def _produce(self, ctx: SlotContext) -> str | None:
        profile: Any = ctx.user_profile
        if not profile:
            return None

        parts: list[str] = []
        name: str | None = getattr(profile, "name", None)
        if name:
            parts.append(f"用户: {name}")

        language: str | None = getattr(profile, "preferred_language", None)
        if language:
            parts.append(f"偏好语言: {language}")

        if not parts:
            return None
        return "\n".join(parts)


# ======================== MemorySlot (tier 2) ========================


class MemorySlot(ContextSlot):
    """语义记忆检索。调用 memory_manager.search() 获取。"""

    name = "memory"
    tier = TierLevel.CONDITIONAL
    cache_policy = "request"

    async def _produce(self, ctx: SlotContext) -> str | None:
        manager: Any = ctx.memory_manager
        if not manager:
            return None

        try:
            results = await manager.search(ctx.query)
        except Exception as e:
            logger.warning("记忆检索失败: %s", e)
            return None

        if not results:
            return None
        # Format SearchResult list into readable Markdown
        lines: list[str] = []
        for r in results:
            title_prefix: str = f"[{r.title}] " if r.title else ""
            lines.append(f"- ({r.source}:{r.path}) {title_prefix}{r.snippet}")
        return "## 相关记忆\n\n" + "\n".join(lines)


# ======================== KnowledgeSlot (tier 2) ========================


class KnowledgeSlot(ContextSlot):
    """外部知识/RAG 检索。调用 knowledge_base.search() 获取。"""

    name = "knowledge"
    tier = TierLevel.CONDITIONAL
    cache_policy = "request"

    async def _produce(self, ctx: SlotContext) -> str | None:
        kb: Any = ctx.knowledge_base
        if not kb:
            return None

        try:
            result: str | None = await kb.search(ctx.query)
        except Exception as e:
            logger.warning("知识库检索失败: %s", e)
            return None

        if not result:
            return None
        return f"## 相关知识\n\n{result}"


# ======================== ProjectSlot (tier 2) ========================


class ProjectSlot(ContextSlot):
    """项目上下文。读取项目根目录下的约定文件。"""

    name = "project"
    tier = TierLevel.CONDITIONAL
    cache_policy = "request"

    # 按优先级排列的候选文件名
    _PROJECT_FILES: list[str] = ["AGENTS.md", "CLAUDE.md", "README.md"]

    async def _produce(self, ctx: SlotContext) -> str | None:
        project_root: Path = ctx.project_root
        if not project_root.exists():
            return None

        contents: list[str] = []
        for filename in self._PROJECT_FILES:
            filepath: Path = project_root / filename
            if not filepath.is_file():
                continue
            try:
                text: str = filepath.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("读取项目文件 %s 失败: %s", filepath, e)
                continue
            if text.strip():
                contents.append(f"## {filename}\n\n{text.strip()}")

        if not contents:
            return None
        return "\n\n".join(contents)


# ======================== AvailableAgentsSlot (tier 0) ========================


class AvailableAgentsSlot(ContextSlot):
    """注入可用子 Agent 列表到 system prompt。

    从 AgentRegistry 读取所有已注册的 Agent Identity，
    以结构化列表形式告知 LLM 可以使用哪些子 Agent。
    同时告知：没有合适 Agent 时使用默认 daily-assistant。
    """

    name = "available_agents"
    tier = TierLevel.STATIC
    cache_policy = "forever"

    async def _produce(self, ctx: SlotContext) -> str | None:
        registry = ctx.agent_registry
        if registry is None:
            return None

        agents = registry.list_all()
        if not agents:
            return None

        lines: list[str] = [
            "## 可用子 Agent",
            "",
            "你可以使用 spawn_agent 工具派生以下子 Agent 来执行子任务。",
            "根据任务性质选择合适的 Agent：",
            "",
        ]
        for a in agents:
            desc: str = a.description or a.agent_name
            caps: str = ", ".join(a.capabilities) if a.capabilities else "通用"
            lines.append(f"- **{a.agent_id}** ({a.agent_name}): {desc}。能力：{caps}")

        lines.append("")
        lines.append(
            "如果没有特别合适的 Agent，使用 **daily-assistant**（通用助手）处理所有类型任务。"
        )
        lines.append(
            "一次可以并行 spawn 多个子 Agent，每个返回独立结果。"
        )
        return "\n".join(lines)
