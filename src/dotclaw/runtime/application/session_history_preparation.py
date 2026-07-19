"""创建 Run 前的 Session 历史压缩与冻结服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.facts import ContextCompactionScope, HistoryCompressionSnapshot, MessageRole
from .context_compaction import ContextCompactionRequest, ContextFragment
from .dto import ConversationMessage, ConversationSnapshot
from .ports import ContextCompactionPort

class ConversationHistoryRecord(Protocol):
    """历史准备服务读取的单条 Conversation 结构。"""

    conversation_id: str
    user_query: str
    final_answer: str
    created_at: str


class HistoryCompressionRecord(Protocol):
    """历史准备服务读取的当前压缩版本结构。"""

    version: int
    covered_through_conversation_id: str
    content: str
    content_hash: str
    created_at: str


class SessionHistoryRecord(Protocol):
    """Session 历史压缩所需的最小可变状态。"""

    id: str
    conversation_version: int
    conversations: list[ConversationHistoryRecord]

    def active_history_compression(self) -> HistoryCompressionRecord | None:
        """返回当前生效的压缩版本。"""

    def append_history_compression(
        self,
        version: int,
        covered_through_conversation_id: str,
        content: str,
        content_hash: str,
        source_conversation_hash: str,
        previous_version: int = 0,
    ) -> None:
        """追加下一版历史压缩记录。"""


class SessionHistoryStore(Protocol):
    """历史准备服务所需的最小 Session 读写协议。"""

    async def load(self, session_id: str) -> SessionHistoryRecord | None:
        """加载指定 Session。"""

    async def save(self, session: SessionHistoryRecord) -> None:
        """原子保存已更新的 Session。"""


class SessionHistoryPreparationError(RuntimeError):
    """历史无法压缩或冻结时阻止创建 Run 的错误。"""


@dataclass(frozen=True)
class HistoryPreparationPolicy:
    """创建 Run 前可用于 Session 历史的预算与轮次限制。"""

    max_context_tokens: int
    max_recent_conversations: int
    reserved_tokens: int

    @property
    def history_token_budget(self) -> int:
        """返回扣除固定上下文预留后的历史 token 预算。"""
        return self.max_context_tokens - self.reserved_tokens


class SessionHistoryPreparationService:
    """在 Run 创建前压缩 Session 历史，并返回不可变 ConversationSnapshot。"""

    def __init__(
        self,
        store: SessionHistoryStore,
        compactor: ContextCompactionPort,
        policy: HistoryPreparationPolicy,
    ) -> None:
        """绑定 Session 存储、压缩 Port 与冻结预算策略。"""
        if policy.history_token_budget <= 0:
            raise ValueError("历史 token 预算必须为正数")
        if policy.max_recent_conversations <= 0:
            raise ValueError("最大保留 Conversation 数必须为正数")
        self._store: SessionHistoryStore = store
        self._compactor: ContextCompactionPort = compactor
        self._policy: HistoryPreparationPolicy = policy

    async def prepare(self, session_id: str) -> ConversationSnapshot:
        """按策略压缩必要历史，并返回当前 Run 应冻结的历史快照。"""
        session: SessionHistoryRecord | None = await self._store.load(session_id)
        if session is None:
            raise SessionHistoryPreparationError(f"Session 不存在：{session_id}")
        if self._needs_compaction(session):
            await self._compact(session)
            try:
                await self._store.save(session)
            except Exception as error:
                raise SessionHistoryPreparationError(f"历史压缩写入失败：{error}") from error
        return self._to_snapshot(session)

    def _needs_compaction(self, session: SessionHistoryRecord) -> bool:
        """根据未压缩轮次和估算 token 判断是否需要生成下一版摘要。"""
        active = session.active_history_compression()
        recent_conversations = _recent_conversations(session, active.covered_through_conversation_id if active is not None else "")
        if len(recent_conversations) > self._policy.max_recent_conversations:
            return True
        return _estimate_history_tokens(session) > self._policy.history_token_budget

    async def _compact(self, session: SessionHistoryRecord) -> None:
        """将可归档历史合成为下一版摘要并更新 Session 内存事实。"""
        active = session.active_history_compression()
        recent_conversations = _recent_conversations(session, active.covered_through_conversation_id if active is not None else "")
        retain_count: int = max(1, min(self._policy.max_recent_conversations, len(recent_conversations) - 1))
        to_compact = recent_conversations[:-retain_count]
        if not to_compact:
            raise SessionHistoryPreparationError("历史超出预算但没有可压缩的 Conversation")
        fragments: list[ContextFragment] = []
        if active is not None:
            fragments.append(ContextFragment(
                fragment_id=f"compression-{active.version}",
                role=MessageRole.SYSTEM,
                content=active.content,
            ))
        for conversation in to_compact:
            fragments.append(ContextFragment(f"{conversation.conversation_id}:user", MessageRole.USER, conversation.user_query))
            fragments.append(ContextFragment(f"{conversation.conversation_id}:assistant", MessageRole.ASSISTANT, conversation.final_answer))
        try:
            result = await self._compactor.compact(ContextCompactionRequest(
                scope=ContextCompactionScope.SESSION_HISTORY,
                source_version=session.conversation_version,
                target_token_budget=self._policy.history_token_budget,
                fragments=tuple(fragments),
                previous_summary=active.content if active is not None else "",
                previous_summary_version=active.version if active is not None else 0,
            ))
        except Exception as error:
            raise SessionHistoryPreparationError(f"历史压缩失败：{error}") from error
        session.append_history_compression(
            version=result.version,
            covered_through_conversation_id=to_compact[-1].conversation_id,
            content=result.content,
            content_hash=result.content_hash,
            source_conversation_hash=result.source_hash,
            previous_version=active.version if active is not None else 0,
        )

    def _to_snapshot(self, session: SessionHistoryRecord) -> ConversationSnapshot:
        """将当前有效摘要和未覆盖 Conversation 转换为冻结应用层快照。"""
        active = session.active_history_compression()
        messages: list[ConversationMessage] = []
        if active is not None:
            messages.append(ConversationMessage(
                message_id=f"history-compression-{active.version}",
                role=MessageRole.SYSTEM,
                content=f"以下是此前对话的压缩摘要：\n{active.content}",
                created_at=active.created_at,
            ))
        for conversation in _recent_conversations(session, active.covered_through_conversation_id if active is not None else ""):
            messages.append(ConversationMessage(f"{conversation.conversation_id}:user", MessageRole.USER, conversation.user_query, conversation.created_at))
            messages.append(ConversationMessage(f"{conversation.conversation_id}:assistant", MessageRole.ASSISTANT, conversation.final_answer, conversation.created_at))
        compressed_history: HistoryCompressionSnapshot | None = None
        if active is not None:
            compressed_history = HistoryCompressionSnapshot(
                compression_version=active.version,
                covered_through_conversation_id=active.covered_through_conversation_id,
                content=active.content,
                content_hash=active.content_hash,
            )
        return ConversationSnapshot(
            session.id,
            tuple(messages),
            session.conversation_version,
            compressed_history,
        )


def _recent_conversations(
    session: SessionHistoryRecord,
    covered_through_conversation_id: str,
) -> list[ConversationHistoryRecord]:
    """返回压缩边界之后的 Conversation；空边界表示全部保留。"""
    if not covered_through_conversation_id:
        return list(session.conversations)
    for index, conversation in enumerate(session.conversations):
        if conversation.conversation_id == covered_through_conversation_id:
            return list(session.conversations[index + 1:])
    raise SessionHistoryPreparationError("当前历史压缩边界不属于 Session Conversation")


def _estimate_history_tokens(session: SessionHistoryRecord) -> int:
    """按中文与英文混合文本的保守四字符近似估算历史 token。"""
    active = session.active_history_compression()
    characters: int = len(active.content) if active is not None else 0
    for conversation in _recent_conversations(session, active.covered_through_conversation_id if active is not None else ""):
        characters += len(conversation.user_query) + len(conversation.final_answer)
    return max((characters + 3) // 4, 1)
