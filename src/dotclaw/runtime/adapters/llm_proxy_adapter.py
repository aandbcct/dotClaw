"""将旧 LLMProxy 适配为 Runtime v3 的 LLMPort。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from ...llm.base import ChatChunk
from ...llm.base import Message as LegacyMessage
from ...llm.base import ToolCall as LegacyToolCall
from ...llm.base import ToolDefinition as LegacyToolDefinition
from ...llm.proxy import LLMProxy
from ..application.dto import ContextBundle
from ..application.ports import LLMPort, TextStreamPort
from dotclaw.runtime.application.execution import RunExecutionView
from ..domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall


class LLMProxyAdapter(LLMPort):
    """聚合旧流式响应并转换为 Runtime v3 完整 RunMessage 的适配器。"""

    def __init__(self, proxy: LLMProxy, text_stream_port: TextStreamPort | None = None) -> None:
        """绑定既有 LLM 代理与可选的入口文本流端口。"""
        self._proxy: LLMProxy = proxy
        self._text_stream_port: TextStreamPort | None = text_stream_port

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """调用旧代理并聚合文本、工具调用与 token 统计。"""
        messages: list[LegacyMessage] = [
            LegacyMessage(
                role=message.role.value,
                content=message.content,
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=[LegacyToolCall(call.call_id, call.name, json.dumps(call.arguments, ensure_ascii=False)) for call in message.tool_calls] or None,
            )
            for message in context.messages
        ]
        tools: list[LegacyToolDefinition] = [
            LegacyToolDefinition(tool.name, tool.description, tool.parameters)
            for tool in context.tools
        ]
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        input_tokens: int = 0
        output_tokens: int = 0
        has_streamed_text: bool = False
        response: AsyncIterator[ChatChunk] = self._proxy.chat(
            messages=messages,
            tools=tools or None,
            model=execution.policy.model_id or None,
            stream=True,
        )
        async for chunk in response:
            if chunk.content:
                content_parts.append(chunk.content)
                if self._text_stream_port is not None:
                    await self._text_stream_port.emit(execution.run_id, chunk.content)
                    has_streamed_text = True
            if chunk.tool_call is not None:
                tool_calls.append(_tool_call_from_legacy(chunk.tool_call))
            if chunk.is_final:
                input_tokens = chunk.input_tokens
                output_tokens = chunk.output_tokens
        return RunMessage(
            message_id=f"llm-{execution.run_id}",
            sequence=0,
            kind=RunMessageKind.LLM_RESPONSE,
            role=MessageRole.ASSISTANT,
            content="".join(content_parts),
            tool_calls=tuple(tool_calls),
            metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "has_streamed_text": has_streamed_text,
            },
        )

    async def cancel(self, run_id: str) -> None:
        """旧 LLMProxy 未公开取消句柄；保留尽力取消协议入口。"""


def _tool_call_from_legacy(call: LegacyToolCall) -> ToolCall:
    """解析旧工具调用 JSON 参数，非法 JSON 映射为空对象。"""
    try:
        parsed = json.loads(call.arguments)
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    arguments = parsed if isinstance(parsed, dict) else {}
    return ToolCall(call.id, call.name, arguments)
