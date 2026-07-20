"""按 Owner 快照加载的内置 Context Slot。"""

from __future__ import annotations

from dotclaw.runtime.domain.context import ContextContributionKind, ContextSlotStatus
from dotclaw.runtime.domain.facts import JSONValue

from .contracts import ContextContribution, ContextSlotBinding
from .signals import ContextRefreshSignal


class _TextOwnerSlot:
    """从精确 Owner 字段读取文本的无状态基础 Slot。"""

    def __init__(self, field_name: str) -> None:
        self._field_name: str = field_name

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """把非空文本字段转换为 system 内容。"""
        value: JSONValue | None = binding.owner_data.get(self._field_name)
        if not isinstance(value, str) or not value:
            return ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY)
        return ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.INCLUDED, value)

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
    """声明工具策略位置；完整 Schema 仅由 ContextBundle.tools 承载。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """工具 Slot 不把 Schema 复制到 system 文本。"""
        return ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY)

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


class HistorySlot:
    """保存 Session Conversation 的结构化审计载荷。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """仅携带 Conversation 载荷，不重复为 system 文本。"""
        conversation: JSONValue | None = binding.owner_data.get("conversation")
        if not isinstance(conversation, dict):
            return ContextContribution(ContextContributionKind.HISTORY, ContextSlotStatus.EMPTY)
        messages: JSONValue | None = conversation.get("messages")
        status: ContextSlotStatus = (
            ContextSlotStatus.INCLUDED if isinstance(messages, list) and messages else ContextSlotStatus.EMPTY
        )
        return ContextContribution(ContextContributionKind.HISTORY, status, attributes={"conversation": conversation})

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """历史数据由 Session Owner 快照决定。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """仅接受当前 Session 的历史变更事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """History Slot 不持有资源。"""


class RunMessagesSlot:
    """仅引用 Run Message 标识，正文始终留在 messages.json。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """转换精确的字符串消息标识列表。"""
        raw_ids: JSONValue | None = binding.owner_data.get("message_ids")
        if not isinstance(raw_ids, list) or not all(isinstance(item, str) for item in raw_ids):
            return ContextContribution(ContextContributionKind.RUN_MESSAGE_REFERENCES, ContextSlotStatus.EMPTY)
        message_ids: tuple[str, ...] = tuple(raw_ids)
        status: ContextSlotStatus = ContextSlotStatus.INCLUDED if message_ids else ContextSlotStatus.EMPTY
        return ContextContribution(ContextContributionKind.RUN_MESSAGE_REFERENCES, status, message_ids=message_ids)

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """Run Message 集合由 Run Owner 快照决定。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """仅接受当前 Run 的消息变更事件。"""
        return _matches_signal(binding, signal)

    async def release(self) -> None:
        """RunMessagesSlot 不持有资源。"""


def _matches_signal(binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
    """校验事件是否精确指向当前 Slot 实例，载荷由具体 Slot 自行判定。"""
    return (
        signal.slot_id == binding.descriptor.slot_id
        and signal.owner is binding.descriptor.owner
        and signal.owner_key == binding.owner_key
    )
