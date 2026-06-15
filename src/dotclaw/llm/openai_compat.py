"""OpenAI 兼容客户端基类

封装 OpenAI 兼容 API 的通用逻辑：
- 消息格式转换（含 tool_calls 序列化）
- 流式 chunk 解析 + tool_calls 参数累积
- 流式状态管理

子类只需覆写三个钩子方法：
- _get_api_key() → str
- _get_base_url() → str
- _get_model_id() → str
"""

from __future__ import annotations

import json
from abc import abstractmethod
from typing import AsyncIterator, Iterator

from openai import AsyncOpenAI

from .base import ChatChunk, LLMClient, Message, ToolCall, ToolDefinition


class OpenAICompatibleClient(LLMClient):
    """
    OpenAI 兼容客户端基类。

    所有继承 OpenAI API 格式的供应商（Qwen、DeepSeek、OpenAI 等）
    共享此实现，仅覆写 provider 特定的钩子。
    """

    def __init__(self):
        self._reset_stream_state()

    # ---- 子类必须覆写的钩子 ----

    @abstractmethod
    def _get_api_key(self) -> str:
        """返回该 provider 的 API key"""
        ...

    @abstractmethod
    def _get_base_url(self) -> str:
        """返回该 provider 的 base URL"""
        ...

    @abstractmethod
    def _get_model_id(self) -> str:
        """返回当前实例绑定的 model 名称"""
        ...

    # ---- 子类可选覆写的钩子 ----

    def _get_client(self) -> AsyncOpenAI:
        """创建 AsyncOpenAI 实例（子类可覆写以注入 custom headers）"""
        assert False, "subclass must implement _get_client"
        return AsyncOpenAI(
            api_key=self._get_api_key(),
            base_url=self._get_base_url(),
        )

    # ---- 流式状态管理 ----

    def _reset_stream_state(self):
        """重置流式解析的临时状态"""
        self._pending_tool_calls: dict[int, dict] = {}  # index → {id, name, arguments}
        self._stream_finish_reason: str = "stop"

    # ---- 核心 chat 方法 ----

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        self._reset_stream_state()

        openai_messages = self._convert_messages(messages)

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

        client = self._get_client()
        params: dict = {
            "model": self._get_model_id(),
            "messages": openai_messages,
            "stream": stream,
        }
        if stream:
            params["stream_options"] = {"include_usage": True}
        if openai_tools:
            params["tools"] = openai_tools

        response = await client.chat.completions.create(**params)

        if stream:
            input_tokens = 0
            output_tokens = 0
            async for chunk in response:
                # 从 usage chunk 提取 token 统计（stream_options 开启时才返回）
                if hasattr(chunk, "usage") and chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0
                for sub in self._parse_stream_chunk(chunk):
                    yield sub
            # 统一 yield 最终的 is_final chunk（携带 tokens + finish_reason）
            yield ChatChunk(content="", is_final=True,
                           finish_reason=self._stream_finish_reason,
                           input_tokens=input_tokens, output_tokens=output_tokens)
        else:
            choice = response.choices[0]
            content = choice.message.content or ""
            usage = getattr(response, "usage", None)
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            yield ChatChunk(content=content, is_final=True,
                           input_tokens=in_tok, output_tokens=out_tok)

    # ---- 消息格式转换 ----

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

    # ---- 流式 chunk 解析 ----

    def _parse_stream_chunk(self, chunk) -> Iterator[ChatChunk]:
        """解析 OpenAI SSE chunk，正确处理跨 chunk 的 arguments 增量拼接。"""
        # usage-only chunk（stream_options 开启时返回），跳过
        if not chunk.choices:
            return
        delta = chunk.choices[0].delta
        content = delta.content or ""

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
                    pending["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    pending["arguments"] += tc.function.arguments

        is_final = chunk.choices[0].finish_reason is not None

        if is_final:
            self._stream_finish_reason = chunk.choices[0].finish_reason
            for idx, pending in self._pending_tool_calls.items():
                if pending["name"]:
                    yield ChatChunk(
                        content="",
                        tool_call=ToolCall(
                            id=pending["id"],
                            name=pending["name"],
                            arguments=pending["arguments"],
                        ),
                    )

            # 只 yield tool_calls，不设置 is_final
            # 由外层 chat() 统一 yield 最终的 is_final  chunk（带 token 信息）
            self._reset_stream_state()
        else:
            if content:
                yield ChatChunk(content=content, tool_call=None)
