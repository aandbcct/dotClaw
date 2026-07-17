"""Runtime v2 的业务 ContextSlot 实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
from enum import StrEnum
from pathlib import Path

from ..runtime.application.dto import ToolDefinition
from .ports import AgentDescriptor, MemorySearchRecord
from .scoped_cache import SlotCacheScope
from .slot_context import SlotContext


class ProjectContextFile(StrEnum):
    """ProjectSlot 可读取的项目约定文件。"""

    AGENTS = "AGENTS.md"
    CLAUDE = "CLAUDE.md"
    README = "README.md"


class ContextSlot(ABC):
    """只负责产生一个上下文片段的抽象槽位。"""

    name: str
    scope: SlotCacheScope

    @abstractmethod
    async def produce(self, context: SlotContext) -> str | None:
        """根据不可变输入产生文本；失败由 Provider 统一降级。"""


class IdentitySlot(ContextSlot):
    """提供冻结后的 Agent system prompt。"""

    name: str = "identity"
    scope: SlotCacheScope = SlotCacheScope.STATIC

    async def produce(self, context: SlotContext) -> str | None:
        """返回身份提示词。"""
        return context.profile.system_prompt or None


class ToolsSlot(ContextSlot):
    """将可用工具定义格式化为 system prompt 片段。"""

    name: str = "tools"
    scope: SlotCacheScope = SlotCacheScope.SESSION

    async def produce(self, context: SlotContext) -> str | None:
        """返回所有可用工具的名称、说明与参数定义。"""
        if not context.profile.tools:
            return None
        lines: list[str] = ["## 可用工具"]
        tool: ToolDefinition
        for tool in context.profile.tools:
            lines.append(f"### {tool.name}")
            if tool.description:
                lines.append(tool.description)
            if tool.parameters:
                parameters_text: str = json.dumps(tool.parameters, ensure_ascii=False, indent=2)
                lines.append(f"参数: {parameters_text}")
        return "\n".join(lines)


class SkillsSlot(ContextSlot):
    """提供技能注册表中的可用技能摘要。"""

    name: str = "skills"
    scope: SlotCacheScope = SlotCacheScope.SESSION

    async def produce(self, context: SlotContext) -> str | None:
        """返回可用技能说明。"""
        registry = context.dependencies.skill_registry
        if registry is None:
            return None
        descriptions: str = registry.get_descriptions_block(max_desc_len=20)
        if not descriptions:
            return None
        return "## 可用技能\n\n" + descriptions


class WorkspaceSlot(ContextSlot):
    """提供当前 Agent 的工作空间路径。"""

    name: str = "workspace"
    scope: SlotCacheScope = SlotCacheScope.SESSION

    async def produce(self, context: SlotContext) -> str | None:
        """返回工作空间信息。"""
        return f"工作空间: {context.profile.project_root}"


class UserInfoSlot(ContextSlot):
    """提供可选用户资料，避免 Slot 直接依赖 Channel 或 Session。"""

    name: str = "user_info"
    scope: SlotCacheScope = SlotCacheScope.SESSION

    async def produce(self, context: SlotContext) -> str | None:
        """返回可展示的用户名和语言偏好。"""
        profile = context.dependencies.user_profile
        if profile is None:
            return None
        lines: list[str] = []
        if profile.name:
            lines.append(f"用户: {profile.name}")
        if profile.preferred_language:
            lines.append(f"偏好语言: {profile.preferred_language}")
        return "\n".join(lines) if lines else None


class MemorySlot(ContextSlot):
    """按当前用户输入检索相关记忆。"""

    name: str = "memory"
    scope: SlotCacheScope = SlotCacheScope.CONDITIONAL

    async def produce(self, context: SlotContext) -> str | None:
        """将记忆检索结果格式化为提示词片段。"""
        memory_manager = context.dependencies.memory_manager
        if memory_manager is None:
            return None
        results = await memory_manager.search(context.query)
        if not results:
            return None
        lines: list[str] = []
        result: MemorySearchRecord
        for result in results:
            title_prefix: str = f"[{result.title}] " if result.title else ""
            lines.append(f"- ({result.source}:{result.path}) {title_prefix}{result.snippet}")
        return "## 相关记忆\n\n" + "\n".join(lines)


class KnowledgeSlot(ContextSlot):
    """按当前用户输入检索外部知识。"""

    name: str = "knowledge"
    scope: SlotCacheScope = SlotCacheScope.CONDITIONAL

    async def produce(self, context: SlotContext) -> str | None:
        """返回知识库检索摘要。"""
        knowledge_base = context.dependencies.knowledge_base
        if knowledge_base is None:
            return None
        result: str | None = await knowledge_base.search(context.query)
        return None if not result else "## 相关知识\n\n" + result


class ProjectSlot(ContextSlot):
    """读取有上限的项目约定文件，避免上下文无限增长。"""

    name: str = "project"
    scope: SlotCacheScope = SlotCacheScope.CONDITIONAL

    async def produce(self, context: SlotContext) -> str | None:
        """读取预算允许范围内的项目说明文件。"""
        remaining_characters: int = max(context.profile.max_context_tokens * 4, 0)
        contents: list[str] = []
        filename: ProjectContextFile
        for filename in ProjectContextFile:
            if remaining_characters <= 0:
                break
            file_path: Path = context.profile.project_root / filename.value
            if not file_path.is_file():
                continue
            text: str = file_path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            clipped_text: str = text[:remaining_characters]
            contents.append(f"## {filename.value}\n\n{clipped_text}")
            remaining_characters -= len(clipped_text)
        return "\n\n".join(contents) if contents else None


class AvailableAgentsSlot(ContextSlot):
    """提供可委托 Agent 的简要目录。"""

    name: str = "available_agents"
    scope: SlotCacheScope = SlotCacheScope.STATIC

    async def produce(self, context: SlotContext) -> str | None:
        """返回可用 Agent 列表。"""
        registry = context.dependencies.agent_registry
        if registry is None:
            return None
        agents = registry.list_all()
        if not agents:
            return None
        lines: list[str] = ["## 可用子 Agent"]
        agent: AgentDescriptor
        for agent in agents:
            description: str = agent.description or agent.agent_name
            capabilities: str = ", ".join(agent.capabilities) if agent.capabilities else "通用"
            lines.append(f"- **{agent.agent_id}** ({agent.agent_name}): {description}。能力：{capabilities}")
        return "\n".join(lines)
