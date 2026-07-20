"""按完整 Conversation 选择与滚动生成历史摘要的应用能力。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .dto import ConversationMessage

if TYPE_CHECKING:
    from .ports import HistoryCompactorPort


@dataclass(frozen=True)
class ConversationBatch:
    """不可拆分的一条完整 Conversation。"""
    conversation_id: str
    messages: tuple[ConversationMessage, ...]
    input_tokens: int


@dataclass(frozen=True)
class HistoryCompactionRequest:
    """一次摘要调用的前摘要与完整 Conversation 批次。"""
    previous_summary: str
    batches: tuple[ConversationBatch, ...]
    source_context_window: int


@dataclass(frozen=True)
class HistoryCompactionResult:
    """摘要器返回的下一段滚动摘要。"""
    summary: str


def select_oldest_conversations(conversations: tuple[ConversationBatch, ...]) -> tuple[ConversationBatch, ...]:
    """选择未压缩部分最旧 75%，并始终保留最新 Conversation 原文。"""
    if len(conversations) <= 1:
        return ()
    candidate_count: int = max(1, (len(conversations) * 3) // 4)
    selected_count: int = min(candidate_count, len(conversations) - 1)
    return conversations[:selected_count]


async def compact_in_batches(
    compactor: HistoryCompactorPort,
    previous_summary: str,
    batches: tuple[ConversationBatch, ...],
    source_context_window: int,
    previous_summary_tokens: int,
) -> HistoryCompactionResult:
    """按精确 Token 计数和模型窗口滚动摘要，绝不拆分 Conversation。"""
    if source_context_window <= 0:
        raise ValueError("压缩模型窗口必须为正数")
    summary: str = previous_summary
    for batch_group in _partition_batches(batches, source_context_window, previous_summary_tokens):
        request: HistoryCompactionRequest = HistoryCompactionRequest(
            previous_summary=summary,
            batches=batch_group,
            source_context_window=source_context_window,
        )
        result: HistoryCompactionResult = await compactor.compact_history(request)
        summary = result.summary
    return HistoryCompactionResult(summary)


def _partition_batches(batches: tuple[ConversationBatch, ...], window: int, summary_tokens: int) -> tuple[tuple[ConversationBatch, ...], ...]:
    """依据已精确统计的 Token 将完整 Conversation 分组。"""
    groups: list[tuple[ConversationBatch, ...]] = []
    current: list[ConversationBatch] = []
    current_tokens: int = summary_tokens
    batch: ConversationBatch
    for batch in batches:
        if batch.input_tokens <= 0 or batch.input_tokens + summary_tokens > window:
            raise ValueError("单条 Conversation 的精确 Token 计数不满足压缩窗口")
        if current and current_tokens + batch.input_tokens > window:
            groups.append(tuple(current))
            current = []
            current_tokens = 0
        current.append(batch)
        current_tokens += batch.input_tokens
    if current:
        groups.append(tuple(current))
    return tuple(groups)
