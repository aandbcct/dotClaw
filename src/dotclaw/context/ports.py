"""上下文层依赖的最小协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..runtime.application.ports import ContextPort
from ..runtime.application.dto import ContextMetadata

__all__ = [
    "AgentDescriptor",
    "AgentDirectoryPort",
    "ContextDependencies",
    "ContextMetadata",
    "ContextPort",
    "KnowledgeSearchPort",
    "MemorySearchPort",
    "MemorySearchRecord",
    "SkillRegistryPort",
    "UserProfile",
]


class MemorySearchRecord(Protocol):
    """记忆检索结果中 ContextSlot 需要的只读字段。"""

    path: str
    snippet: str
    source: str
    title: str


class MemorySearchPort(Protocol):
    """为 MemorySlot 提供语义检索能力。"""

    async def search(self, query: str) -> Sequence[MemorySearchRecord]:
        """按用户输入检索相关记忆。"""


class KnowledgeSearchPort(Protocol):
    """为 KnowledgeSlot 提供外部知识检索能力。"""

    async def search(self, query: str) -> str | None:
        """按用户输入返回可注入的知识摘要。"""


class SkillRegistryPort(Protocol):
    """为 SkillsSlot 提供技能描述摘要。"""

    def get_descriptions_block(self, max_desc_len: int) -> str:
        """返回限制长度后的技能描述文本。"""


class AgentDescriptor(Protocol):
    """AvailableAgentsSlot 所需的 Agent 只读描述。"""

    agent_id: str
    agent_name: str
    description: str
    capabilities: list[str]


class AgentDirectoryPort(Protocol):
    """为 AvailableAgentsSlot 提供可委托 Agent 列表。"""

    def list_all(self) -> Sequence[AgentDescriptor]:
        """返回所有可见 Agent。"""


@dataclass(frozen=True)
class UserProfile:
    """可选用户资料的最小展示字段。"""

    name: str = ""
    preferred_language: str = ""


@dataclass(frozen=True)
class ContextDependencies:
    """ContextPort 可使用的可选外部内容来源。"""

    skill_registry: SkillRegistryPort | None = None
    memory_manager: MemorySearchPort | None = None
    knowledge_base: KnowledgeSearchPort | None = None
    user_profile: UserProfile | None = None
    agent_registry: AgentDirectoryPort | None = None
