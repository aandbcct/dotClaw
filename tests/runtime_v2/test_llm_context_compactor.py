"""LLMContextCompactor 的请求映射与摘要结果测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from dotclaw.llm.base import ChatChunk, Message, ToolDefinition
from dotclaw.runtime.adapters.llm_context_compactor import LLMContextCompactor
from dotclaw.runtime.application.context_compaction import ContextCompactionRequest, ContextCompactionResult, ContextFragment
from dotclaw.runtime.domain.facts import ContextCompactionScope, MessageRole


class CompressionProxy:
    """记录压缩调用参数并返回两段确定摘要的 LLMProxy 替身。"""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.tools: list[ToolDefinition] | None = None
        self.stream: bool = True
        self.purpose: str = ""

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        model: str | None = None,
        purpose: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        """记录压缩调用，不使用模型、用途和工具副作用。"""
        self.messages = messages
        self.tools = tools
        self.stream = stream
        self.purpose = purpose
        yield ChatChunk(content="保留目标和约束。")
        yield ChatChunk(content="保留工具结论。", is_final=True)


async def test_llm_context_compactor_uses_non_streaming_tool_free_call() -> None:
    """压缩器只发送摘要提示和片段，不暴露工具，也不将输出流式交给入口。"""
    proxy: CompressionProxy = CompressionProxy()
    compactor: LLMContextCompactor = LLMContextCompactor(proxy)
    request: ContextCompactionRequest = ContextCompactionRequest(
        scope=ContextCompactionScope.SESSION_HISTORY,
        source_version=3,
        target_token_budget=1200,
        fragments=(
            ContextFragment("conversation-1:user", MessageRole.USER, "用户目标"),
            ContextFragment("conversation-1:assistant", MessageRole.ASSISTANT, "已有结论"),
        ),
    )

    result: ContextCompactionResult = await compactor.compact(request)

    assert proxy.tools is None
    assert proxy.stream is False
    assert proxy.purpose == "context_compaction"
    assert proxy.messages[0].role == "system"
    assert result.scope is ContextCompactionScope.SESSION_HISTORY
    assert result.version == 1
    assert result.covered_through_fragment_id == "conversation-1:assistant"
    assert result.content == "保留目标和约束。保留工具结论。"
    assert result.content_hash.startswith("sha256:")
    assert result.source_hash.startswith("sha256:")
