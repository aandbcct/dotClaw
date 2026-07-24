"""将旧 LLMProxy 适配为 Runtime v4 的 LLMPort。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from ...llm.base import ChatChunk
from ...llm.base import Message as LegacyMessage
from ...llm.base import ToolCall as LegacyToolCall
from ...llm.base import ToolDefinition as LegacyToolDefinition
from ...llm.proxy import LLMProxy
from ..application.dto import ContextBundle
from ..application.ports import LLMPort, LLMUnavailableError, TextStreamPort
from dotclaw.runtime.application.execution import RunExecutionView
from ..domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall


class LLMProxyAdapter(LLMPort):
    """聚合旧流式响应并转换为 Runtime v4 完整 RunMessage 的适配器。"""

    def __init__(self, proxy: LLMProxy) -> None:
        """绑定既有 LLM 代理；文本流端口改为每次 complete 的运行级参数。"""
        self._proxy: LLMProxy = proxy

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        text_stream_port: TextStreamPort | None = None,
    ) -> RunMessage:
        """调用旧代理并聚合文本、工具调用与 token 统计；向运行级端口发射文本增量。"""
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
        try:
            async for chunk in response:
                for delta in chunk.text_deltas:
                    content_parts.append(delta.content)
                    if text_stream_port is not None:
                        await text_stream_port.emit(execution.run_id, delta.content)
                        has_streamed_text = True
                if chunk.tool_calls:
                    tool_calls.extend(_tool_call_from_legacy(tc) for tc in chunk.tool_calls)
                if chunk.finish_reason is not None and chunk.usage is not None:
                    input_tokens = chunk.usage.input_tokens
                    output_tokens = chunk.usage.output_tokens
        except Exception as error:
            raise LLMUnavailableError("业务模型服务不可用") from error
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
