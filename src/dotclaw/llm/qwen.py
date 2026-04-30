"""千问（Qwen）LLM 客户端实现"""

from __future__ import annotations

import json
from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import ChatChunk, LLMClient, Message, ToolCall, ToolDefinition


class QwenClient(LLMClient):
    """
    千问 API 客户端。

    千问兼容 OpenAI SDK，只需改 base_url 和 model。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen-plus",
    ):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        # 转换 messages 格式
        openai_messages = self._convert_messages(messages)

        # 转换 tools 格式（OpenAI 格式）
        openai_tools = None
        if tools:
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        params: dict = {
            "model": self._model,
            "messages": openai_messages,
            "stream": stream,
        }
        if openai_tools:
            params["tools"] = openai_tools

        response = await self._client.chat.completions.create(**params)

        if stream:
            async for chunk in response:
                yield from self._parse_stream_chunk(chunk)
        else:
            choice = response.choices[0]
            content = choice.message.content or ""
            yield ChatChunk(content=content, is_final=True)

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """将 dotClaw Message 转换为 OpenAI 格式"""
        result = []
        for msg in messages:
            m: dict = {"role": msg.role, "content": msg.content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            result.append(m)
        return result

    def _parse_stream_chunk(self, chunk) -> AsyncIterator[ChatChunk]:
        """解析 OpenAI SSE chunk"""
        delta = chunk.choices[0].delta
        content = delta.content or ""

        tool_call = None
        if delta.tool_calls:
            tc = delta.tool_calls[0]
            tool_call = ToolCall(
                id=tc.id or "",
                name=tc.function.name,
                arguments=tc.function.arguments or "",
            )

        if content or tool_call:
            yield ChatChunk(content=content, tool_call=tool_call)

        if chunk.choices[0].finish_reason:
            yield ChatChunk(is_final=True)
