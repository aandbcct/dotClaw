"""精确 Token 统计与历史压缩的应用层数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .dto import ConversationMessage, ToolDefinition
from ..domain.facts import RunMessage


class TokenCountErrorCode(StrEnum):
    """Token 统计失败的确定性类别。"""
    TOKENIZER_UNAVAILABLE = "tokenizer_unavailable"
    INVALID_REQUEST = "invalid_request"


class ContextBudgetStatus(StrEnum):
    """上下文预算规划的结论。"""
    WITHIN_BUDGET = "within_budget"
    COMPACTION_REQUIRED = "compaction_required"
    REJECTED = "rejected"


@dataclass(frozen=True)
class TokenCountRequest:
    """一次真实 LLM 输入的全部可计数组成部分。"""
    tokenizer_encoding: str
    system_contents: tuple[str, ...]
    history_summary: str
    history_messages: tuple[ConversationMessage, ...]
    current_user_message: ConversationMessage
    run_messages: tuple[RunMessage, ...]
    tools: tuple[ToolDefinition, ...]
    protocol_overhead_tokens: int


@dataclass(frozen=True)
class TokenCountResult:
    """精确 Token 计数结果或不含正文的确定性错误。"""
    input_tokens: int
    error_code: TokenCountErrorCode | None = None
    warning: str = ""


@dataclass(frozen=True)
class ContextBudgetDecision:
    """预算判断结果，E2 仅定义契约而不驱动 Engine 时序。"""
    status: ContextBudgetStatus
    input_tokens: int
    context_window: int
    reason: str = ""
