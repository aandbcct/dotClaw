"""LLMProxyAdapter 对旧流式 LLM 的转换测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from dotclaw.llm.base import ChatChunk, ToolCall as LegacyToolCall
from dotclaw.runtime.adapters import LLMProxyAdapter
from dotclaw.runtime.application.execution import RunBudget, RunExecutionView
from dotclaw.runtime.domain.models import AgentPolicySnapshot, ContextBundle, ContextMetadata, MessageRole, RunMessage, RunMessageKind, ToolDefinition
from dotclaw.runtime.domain.state import AgentState


class StreamingProxy:
    """记录旧代理输入并返回混合文本与工具调用的替身。"""

    def __init__(self) -> None:
        self.messages = []
        self.tools = []
        self.model = ""

    async def chat(self, messages, tools, model, stream) -> AsyncIterator[ChatChunk]:
        """模拟旧 LLMProxy.chat 的流式输出。"""
        self.messages = messages
        self.tools = tools
        self.model = model
        yield ChatChunk(content="你好，")
        yield ChatChunk(tool_call=LegacyToolCall("call-1", "weather", '{"city":"上海"}'))
        yield ChatChunk(content="请稍候", is_final=True, input_tokens=12, output_tokens=4)


async def test_llm_proxy_adapter_converts_context_and_aggregates_chunks() -> None:
    """完整上下文、工具定义、工具调用和 token 统计均转换到 v2 模型。"""
    proxy = StreamingProxy()
    port: LLMProxyAdapter = LLMProxyAdapter(proxy)  # type: ignore[arg-type]
    context = ContextBundle(
        messages=(RunMessage("m1", 1, RunMessageKind.LLM_REQUEST, MessageRole.USER, "查天气"),),
        tools=(ToolDefinition("weather", "查询天气", {"type": "object"}),),
        metadata=ContextMetadata(1),
    )
    execution = RunExecutionView("run-1", AgentPolicySnapshot("agent", "v1", "model-x", 3), AgentState(), RunBudget(3), 0, None)

    message = await port.complete(context, execution)

    assert proxy.messages[0].content == "查天气"
    assert proxy.tools[0].name == "weather"
    assert proxy.model == "model-x"
    assert message.content == "你好，请稍候"
    assert message.tool_calls[0].arguments == {"city": "上海"}
    assert message.metadata == {"input_tokens": 12, "output_tokens": 4}
