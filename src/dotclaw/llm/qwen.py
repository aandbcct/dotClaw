"""千问（Qwen）LLM 客户端实现"""

from __future__ import annotations

import json
from typing import AsyncIterator, Iterator

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
        self._reset_stream_state()

    def _reset_stream_state(self):
        """重置流式解析的临时状态"""
        self._pending_tool_calls: dict[int, dict] = {}  # index → {id, name, arguments}

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        # 重置流式解析状态（每次新对话都清空）
        self._reset_stream_state()

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
                for sub in self._parse_stream_chunk(chunk):
                    yield sub
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
            if msg.tool_calls:
                m["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            result.append(m)
        return result

    def _parse_stream_chunk(self, chunk) -> Iterator[ChatChunk]:
        """解析 OpenAI SSE chunk，正确处理跨 chunk 的 arguments 增量拼接"""
        delta = chunk.choices[0].delta

        # 处理文本内容
        content = delta.content or ""

        # 处理 tool_calls（可能多个，可能跨 chunk）
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index if tc.index is not None else 0
                if idx not in self._pending_tool_calls:
                    self._pending_tool_calls[idx] = {
                        "id": "",
                        "name": "",
                        "arguments": "",
                    }

                pending = self._pending_tool_calls[idx]

                if tc.id:
                    pending["id"] = tc.id
                if tc.function and tc.function.name:
                    # name 也是增量传输，需要拼接
                    pending["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    pending["arguments"] += tc.function.arguments

        # 检查是否是最后一个 chunk
        is_final = chunk.choices[0].finish_reason is not None

        if is_final:
            # 产出所有累积的 tool_calls
            for idx, pending in self._pending_tool_calls.items():
                if pending["name"]:  # 忽略只有 content 的情况
                    yield ChatChunk(
                        content="",
                        tool_call=ToolCall(
                            id=pending["id"],
                            name=pending["name"],
                            arguments=pending["arguments"],
                        ),
                    )

            yield ChatChunk(content="", is_final=True)
            self._reset_stream_state()
        else:
            # 非最终 chunk，只产出文本内容
            if content:
                yield ChatChunk(content=content, tool_call=None)
