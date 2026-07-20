"""精确 Token 统计与历史压缩的应用层数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .dto import ConversationMessage, ToolDefinition
from ..domain.facts import JSONMap, RunMessage

if TYPE_CHECKING:
    from .ports import TokenCounterPort


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

    def to_dict(self) -> JSONMap:
        """转换为检查点可保存且不含请求正文的预算决策。"""
        return {
            "status": self.status.value,
            "input_tokens": self.input_tokens,
            "context_window": self.context_window,
            "reason": self.reason,
        }


class ContextBudgetPlanner:
    """以精确 Token 统计结果决定当前业务模型调用是否需要压缩。"""

    def __init__(self, token_counter: TokenCounterPort) -> None:
        """绑定唯一 Token 计数 Port，不提供字符数回退。"""
        self._token_counter: TokenCounterPort = token_counter

    async def plan(
        self,
        token_request: TokenCountRequest,
        context_window: int,
    ) -> ContextBudgetDecision:
        """统计实际输入并返回继续、压缩或确定性拒绝结论。"""
        if context_window <= 0:
            return ContextBudgetDecision(ContextBudgetStatus.REJECTED, 0, context_window, "上下文窗口必须为正数")
        result: TokenCountResult = await self._token_counter.count(token_request)
        if result.error_code is not None:
            return ContextBudgetDecision(
                ContextBudgetStatus.REJECTED,
                result.input_tokens,
                context_window,
                result.error_code.value,
            )
        if result.input_tokens > context_window:
            return ContextBudgetDecision(
                ContextBudgetStatus.COMPACTION_REQUIRED,
                result.input_tokens,
                context_window,
                "实际输入超过模型上下文窗口",
            )
        return ContextBudgetDecision(ContextBudgetStatus.WITHIN_BUDGET, result.input_tokens, context_window)
