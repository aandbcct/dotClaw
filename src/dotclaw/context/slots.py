"""按 Owner 快照加载的内置 Context Slot。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextSlotStatus,
    ConversationMessagesSlotContent,
    ConversationSlotMessage,
    RunMessageReferencesSlotContent,
    TextSlotContent,
    ToolDefinitionSlotContent,
    ToolDefinitionsSlotContent,
)
from dotclaw.runtime.domain.facts import JSONMap, JSONValue, MessageRole

from .contracts import ContextContribution, ContextSlotBinding
from .signals import ContextRefreshSignal


class _TextOwnerSlot:
    """从精确 Owner 字段读取文本的无状态基础 Slot。"""

    def __init__(self, field_name: str) -> None:
        self._field_name: str = field_name

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """把非空文本字段转换为直接文本载荷。"""
        value: JSONValue | None = binding.owner_data.get(self._field_name)
        text: str = value if isinstance(value, str) else ""
        return ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.INCLUDED if text else ContextSlotStatus.EMPTY, TextSlotContent(text))

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """内置文本 Slot 不保存内容缓存。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """内置文本 Slot 仅接受自己的精确 Owner 定向事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """内置文本 Slot 不持有外部资源。"""


class IdentitySlot(_TextOwnerSlot):
    """读取 Agent 冻结身份提示词。"""
    def __init__(self) -> None:
        super().__init__("system_prompt")


class SkillsSlot(_TextOwnerSlot):
    """读取 Agent 所属技能摘要。"""
    def __init__(self) -> None:
        super().__init__("skills_text")


class ToolsSlot:
    """读取 Agent 已筛选的实际工具 Schema。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """将 Agent 策略中的有效工具定义写入直接 Slot 内容。"""
        raw_tools: JSONValue | None = binding.owner_data.get("tools")
        tools: list[ToolDefinitionSlotContent] = []
        if isinstance(raw_tools, list):
            for raw_tool in raw_tools:
                if not isinstance(raw_tool, dict):
                    continue
                name: JSONValue | None = raw_tool.get("name")
                description: JSONValue | None = raw_tool.get("description")
                parameters: JSONValue | None = raw_tool.get("parameters")
                if isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict):
                    tools.append(ToolDefinitionSlotContent(name, description, parameters))
        content: ToolDefinitionsSlotContent = ToolDefinitionsSlotContent(tuple(tools))
        return ContextContribution(ContextContributionKind.TOOL_DEFINITIONS, ContextSlotStatus.INCLUDED if tools else ContextSlotStatus.EMPTY, content)

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """工具定义来自每次冻结的 Agent 策略。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """仅接受当前 Agent 的工具策略刷新事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """工具 Slot 不持有资源。"""


class UserInfoSlot(_TextOwnerSlot):
    """读取 Session 关联用户资料。"""
    def __init__(self) -> None:
        super().__init__("user_info_text")


class MemorySlot(_TextOwnerSlot):
    """读取 Run 检索出的相关记忆。"""
    def __init__(self) -> None:
        super().__init__("memory_text")


class KnowledgeSlot(_TextOwnerSlot):
    """读取 Run 检索出的知识摘要。"""
    def __init__(self) -> None:
        super().__init__("knowledge_text")


class AvailableAgentsSlot(_TextOwnerSlot):
    """读取全局 Agent 目录摘要。"""
    def __init__(self) -> None:
        super().__init__("available_agents_text")


class HistoryCompressionsSlot:
    """保存当前唯一有效的历史摘要正文。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """staged 摘要已由 Provider 以优先级覆盖 Session 摘要。"""
        value: JSONValue | None = binding.owner_data.get("history_compression")
        text: str = value if isinstance(value, str) else ""
        return ContextContribution(ContextContributionKind.HISTORY_COMPRESSIONS, ContextSlotStatus.INCLUDED if text else ContextSlotStatus.EMPTY, TextSlotContent(text))

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """摘要由 Session/Run 冻结快照决定。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """仅接受当前 Session 的历史变更事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """历史摘要 Slot 不持有资源。"""


class ConversationSlot:
    """保存压缩边界之后完整且可审计的 Conversation。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """将冻结 Conversation DTO 转换为封闭 Slot DTO。"""
        raw_messages: JSONValue | None = binding.owner_data.get("conversation_messages")
        messages: list[ConversationSlotMessage] = []
        if isinstance(raw_messages, list):
            for raw_message in raw_messages:
                if not isinstance(raw_message, dict):
                    continue
                message: JSONMap = raw_message
                raw_id: JSONValue | None = message.get("id")
                role: JSONValue | None = message.get("role")
                content: JSONValue | None = message.get("content")
                created_at: JSONValue | None = message.get("created_at")
                if all(isinstance(value, str) for value in (raw_id, role, content, created_at)):
                    messages.append(ConversationSlotMessage(raw_id, MessageRole(role), content, created_at))
        return ContextContribution(ContextContributionKind.CONVERSATION_MESSAGES, ContextSlotStatus.INCLUDED if messages else ContextSlotStatus.EMPTY, ConversationMessagesSlotContent(tuple(messages)))

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """Conversation 在 Run 创建后保持冻结。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """仅接受当前 Run 的冻结 Conversation 刷新事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """Conversation Slot 不持有资源。"""


class RunMessagesSlot:
    """仅引用 Run Message 标识，正文始终留在 messages.json。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """转换精确的字符串消息标识列表。"""
        raw_ids: JSONValue | None = binding.owner_data.get("message_ids")
        ids: tuple[str, ...] = tuple(raw_ids) if isinstance(raw_ids, list) and all(isinstance(item, str) for item in raw_ids) else ()
        return ContextContribution(ContextContributionKind.RUN_MESSAGE_REFERENCES, ContextSlotStatus.INCLUDED if ids else ContextSlotStatus.EMPTY, RunMessageReferencesSlotContent(ids))

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """Run Message 集合由 Run Owner 快照决定。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """仅接受当前 Run 的消息变更事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """RunMessagesSlot 不持有资源。"""


def _matches_signal(binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
    """校验事件是否精确指向当前 Slot 实例。"""
    return signal.slot_id == binding.descriptor.slot_id and signal.owner is binding.descriptor.owner and signal.owner_key == binding.owner_key
