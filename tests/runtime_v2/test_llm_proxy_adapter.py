"""LLMProxyAdapter 对旧流式 LLM 的转换测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from dotclaw.llm.base import ChatChunk, ChatTextDelta, TextDeltaKind, TokenUsage, ToolCall as LegacyToolCall
from dotclaw.runtime.adapters import LLMProxyAdapter
from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, LLMOutputEvent, LLMOutputKind, ToolDefinition
from dotclaw.runtime.application.execution import RunBudget, RunExecutionView
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind
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
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "你好，"),))
        yield ChatChunk(tool_calls=(LegacyToolCall("call-1", "weather", '{"city":"上海"}'),))
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "请稍候"),), finish_reason="stop", usage=TokenUsage(12, 4))


class MixedProxy:
    """先思考后回复的替身，覆盖 reasoning 与 response 双通道。"""

    async def chat(self, messages, tools, model, stream) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.REASONING, "让我想想"),))
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "查到了"),))
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.REASONING, "再确认一下"),))
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "上海晴"),), finish_reason="stop", usage=TokenUsage(20, 6))


class ReasoningOnlyProxy:
    """只输出思考过程、不输出 response 的替身。"""

    async def chat(self, messages, tools, model, stream) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.REASONING, "思考中"),))
        yield ChatChunk(finish_reason="stop", usage=TokenUsage(8, 2))


class CollectingLLMOutputPort:
    """收集入口层应立即收到的语义化增量事件。"""

    def __init__(self) -> None:
        self.events: list[LLMOutputEvent] = []

    async def emit(self, event: LLMOutputEvent) -> None:
        """记录转发的增量事件。"""
        self.events.append(event)


def _make_execution(run_id: str, session_id: str = "") -> RunExecutionView:
    """构造测试用执行视图；session_id 缺省为空字符串（与 RunExecution.view 一致）。"""
    return RunExecutionView(
        run_id,
        AgentPolicySnapshot("agent", "v1", "model-x", 3),
        AgentState(),
        RunBudget(3),
        0,
        None,
        session_id=session_id,
    )


def _make_context() -> ContextBundle:
    """构造最小可用上下文。"""
    return ContextBundle(
        messages=(RunMessage("m1", 1, RunMessageKind.LLM_REQUEST, MessageRole.USER, "查天气"),),
        tools=(ToolDefinition("weather", "查询天气", {"type": "object"}),),
        metadata=ContextMetadata(1),
    )


async def test_llm_proxy_adapter_converts_context_and_aggregates_chunks() -> None:
    """完整上下文、工具定义、工具调用、token 与 response 文本流均转换到 v2 模型。"""
    proxy = StreamingProxy()
    collector: CollectingLLMOutputPort = CollectingLLMOutputPort()
    port: LLMProxyAdapter = LLMProxyAdapter(proxy)
    context = _make_context()
    execution = _make_execution("run-1")

    message = await port.complete(context, execution, collector)

    assert proxy.messages[0].content == "查天气"
    assert proxy.tools[0].name == "weather"
    assert proxy.model == "model-x"
    assert message.content == "你好，请稍候"
    assert message.tool_calls[0].arguments == {"city": "上海"}
    assert message.metadata == {"input_tokens": 12, "output_tokens": 4, "has_streamed_response": True}
    # 仅 response delta 进入聚合内容，且每个事件携带 session/run/kind/content。
    assert [(event.kind.value, event.content) for event in collector.events] == [
        ("response_delta", "你好，"),
        ("response_delta", "请稍候"),
    ]
    assert all(event.session_id == "" for event in collector.events)
    assert all(event.run_id == "run-1" for event in collector.events)


async def test_llm_proxy_adapter_emits_reasoning_but_excludes_from_message() -> None:
    """reasoning delta 必须发射给入口，但既不进入最终消息内容也不计入聚合。"""
    collector: CollectingLLMOutputPort = CollectingLLMOutputPort()
    port: LLMProxyAdapter = LLMProxyAdapter(MixedProxy())
    context = _make_context()
    execution = _make_execution("run-2")

    message = await port.complete(context, execution, collector)

    # 思考文本绝不进入最终消息，只有 response 参与聚合。
    assert message.content == "查到了上海晴"
    assert "让我想想" not in message.content
    assert "再确认一下" not in message.content
    assert message.metadata["has_streamed_response"] is True
    assert [(event.kind.value, event.content) for event in collector.events] == [
        ("reasoning_delta", "让我想想"),
        ("response_delta", "查到了"),
        ("reasoning_delta", "再确认一下"),
        ("response_delta", "上海晴"),
    ]


async def test_llm_proxy_adapter_reasoning_only_keeps_message_empty() -> None:
    """仅 reasoning 输出时最终消息为空，且未向入口标记 response 已流式输出。"""
    collector: CollectingLLMOutputPort = CollectingLLMOutputPort()
    port: LLMProxyAdapter = LLMProxyAdapter(ReasoningOnlyProxy())
    context = _make_context()
    execution = _make_execution("run-3")

    message = await port.complete(context, execution, collector)

    assert message.content == ""
    assert message.metadata["has_streamed_response"] is False
    assert [(event.kind.value, event.content) for event in collector.events] == [
        ("reasoning_delta", "思考中"),
    ]
