"""E2 TokenCounter 与历史压缩批次契约测试。"""

from __future__ import annotations

from dotclaw.runtime.adapters.tiktoken_token_counter import TiktokenTokenCounter
from dotclaw.runtime.application.context_budget import TokenCountErrorCode, TokenCountRequest
from dotclaw.runtime.application.dto import ConversationMessage, ToolDefinition
from dotclaw.runtime.application.history_compaction import ConversationBatch, compact_in_batches, select_oldest_conversations
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind


def _message(message_id: str, role: MessageRole, content: str) -> ConversationMessage:
    """构造完整 Conversation 消息。"""
    return ConversationMessage(message_id, role, content, "")


async def test_token_counter_rejects_unavailable_encoding_without_prompt_logging() -> None:
    """缺失 Tokenizer 返回确定性错误，警告文本不包含 prompt 正文。"""
    request: TokenCountRequest = TokenCountRequest(
        tokenizer_encoding="missing-e2-encoding",
        system_contents=("系统秘密内容",),
        history_summary="历史秘密内容",
        history_messages=(_message("history", MessageRole.USER, "历史输入"),),
        current_user_message=_message("user", MessageRole.USER, "当前秘密输入"),
        run_messages=(RunMessage("run", 1, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "工具秘密输出"),),
        tools=(ToolDefinition("lookup", "查询", {"type": "string"}),),
        protocol_overhead_tokens=3,
    )
    result = await TiktokenTokenCounter().count(request)
    assert result.error_code is TokenCountErrorCode.TOKENIZER_UNAVAILABLE
    assert "秘密" not in result.warning


def test_select_oldest_seventy_five_percent_keeps_latest_conversation() -> None:
    """75% 选择仅处理最旧完整 Conversation，始终保留最新原文。"""
    batches: tuple[ConversationBatch, ...] = tuple(
        ConversationBatch(f"conversation-{index}", (_message(str(index), MessageRole.USER, str(index)),))
        for index in range(1, 5)
    )
    selected: tuple[ConversationBatch, ...] = select_oldest_conversations(batches)
    assert [batch.conversation_id for batch in selected] == ["conversation-1", "conversation-2", "conversation-3"]
    assert select_oldest_conversations(batches[:1]) == ()


class ScriptedHistoryCompactor:
    """记录滚动输入并返回预设摘要的 Fake。"""
    def __init__(self) -> None:
        self.requests: list[HistoryCompactionRequest] = []
    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        self.requests.append(request)
        return HistoryCompactionResult(f"摘要-{len(self.requests)}")


async def test_rolling_compaction_preserves_batches_and_passes_previous_summary() -> None:
    """滚动压缩按完整 Conversation 分批，并将上一批摘要传给下一批。"""
    fake: ScriptedHistoryCompactor = ScriptedHistoryCompactor()
    batches: tuple[ConversationBatch, ...] = tuple(
        ConversationBatch(f"conversation-{index}", (_message(str(index), MessageRole.USER, str(index)),))
        for index in range(1, 4)
    )
    result: HistoryCompactionResult = await compact_in_batches(fake, "初始摘要", batches, 100, 2)
    assert result.summary == "摘要-2"
    assert [batch.conversation_id for batch in fake.requests[0].batches] == ["conversation-1", "conversation-2"]
    assert [batch.conversation_id for batch in fake.requests[1].batches] == ["conversation-3"]
    assert fake.requests[1].previous_summary == "摘要-1"
