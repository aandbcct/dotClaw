"""按完整 Conversation 选择与滚动生成历史摘要的应用能力。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .dto import ConversationMessage
from .context_budget import TokenCountRequest
from ..domain.facts import MessageRole

if TYPE_CHECKING:
    from .ports import HistoryCompactorPort, TokenCounterPort


class HistoryCompactorUnavailable(RuntimeError):
    """压缩模型服务重试耗尽后的可恢复外部错误。"""


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
    token_counter: TokenCounterPort,
    previous_summary: str,
    batches: tuple[ConversationBatch, ...],
    source_context_window: int,
    tokenizer_encoding: str,
) -> HistoryCompactionResult:
    """按精确 Token 计数和模型窗口滚动摘要，绝不拆分 Conversation。"""
    if source_context_window <= 0:
        raise ValueError("压缩模型窗口必须为正数")
    summary: str = previous_summary
    remaining: tuple[ConversationBatch, ...] = batches
    while remaining:
        summary_tokens: int = await _count_summary_tokens(token_counter, tokenizer_encoding, summary)
        batch_group, remaining = _take_window_batches(remaining, source_context_window, summary_tokens)
        request: HistoryCompactionRequest = HistoryCompactionRequest(
            previous_summary=summary,
            batches=batch_group,
            source_context_window=source_context_window,
        )
        result: HistoryCompactionResult = await compactor.compact_history(request)
        summary = result.summary
    return HistoryCompactionResult(summary)


async def _count_summary_tokens(counter: TokenCounterPort, encoding: str, summary: str) -> int:
    """使用同一精确 TokenCounter 重计数滚动摘要。"""
    result = await counter.count(TokenCountRequest(
        tokenizer_encoding=encoding,
        system_contents=(summary,) if summary else (),
        history_summary="",
        history_messages=(),
        current_user_message=ConversationMessage("summary-counter", MessageRole.USER, "", ""),
        run_messages=(),
        tools=(),
        protocol_overhead_tokens=0,
    ))
    if result.error_code is not None:
        raise RuntimeError(f"滚动摘要 Token 计数失败：{result.error_code.value}")
    return result.input_tokens


def _take_window_batches(batches: tuple[ConversationBatch, ...], window: int, summary_tokens: int) -> tuple[tuple[ConversationBatch, ...], tuple[ConversationBatch, ...]]:
    """取出当前摘要加完整 Conversation 后仍不超窗口的最大前缀。"""
    current: list[ConversationBatch] = []
    current_tokens: int = summary_tokens
    batch: ConversationBatch
    batch: ConversationBatch
    for batch in batches:
        if batch.input_tokens <= 0 or batch.input_tokens + summary_tokens > window:
            raise ValueError("单条 Conversation 的精确 Token 计数不满足压缩窗口")
        if current and current_tokens + batch.input_tokens > window:
            break
        current.append(batch)
        current_tokens += batch.input_tokens
    selected_count: int = len(current)
    return tuple(current), batches[selected_count:]
