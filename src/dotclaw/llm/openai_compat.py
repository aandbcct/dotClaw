"""OpenAI 兼容客户端基类

封装 OpenAI 兼容 API 的通用逻辑：
- 消息格式转换（含 tool_calls 序列化）
- 流式 chunk 解析 + tool_calls 参数累积（按推理模式分离 reasoning / response）
- 请求级流式状态（每次 chat() 局部创建，并发调用互不串线）

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

from .base import (
    ChatChunk,
    ChatTextDelta,
    LLMClient,
    Message,
    TextDeltaKind,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from .reasoning import ReasoningMode, ReasoningPolicy, ReasoningStreamParser


class _StreamParseState:
    """单次 chat() 调用的局部解析状态（替代原 Client 实例级流状态）。

    仅存活于一次 chat() 调用内，调用结束即释放；并发或交错调用各自持有
    独立状态，工具参数、finish reason、标签缓冲与 token 用量互不串线。
    Client 本身只保存不可变 ReasoningPolicy，绝不保存请求级状态。
    """

    def __init__(self) -> None:
        self.pending_tool_calls: dict[int, dict] = {}  # index → {id, name, arguments}
        self.finish_reason: str = "stop"
        self.input_tokens: int = 0
        self.output_tokens: int = 0


class OpenAICompatibleClient(LLMClient):
    """
    OpenAI 兼容客户端基类。

    所有继承 OpenAI API 格式的供应商（Qwen、DeepSeek、OpenAI 等）
    共享此实现，仅覆写 provider 特定的钩子。

    推理模式由注入的不可变 ReasoningPolicy 决定：
    - none：content 原样归为 response；
    - native：从 delta.reasoning_content 提取 reasoning，content 归为 response；
    - tags：仅解析 content，按标签切分 reasoning 与 response。
    """

    def __init__(self, policy: ReasoningPolicy | None = None) -> None:
        # Policy 为不可变策略，由 ModelRouter 从 ModelReasoningConfig 转换注入；
        # Client 仅保存策略，不保存任何请求级流状态（请求级状态在 chat() 内局部创建）。
        self._policy = policy or ReasoningPolicy()

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

    # ---- 核心 embed 方法 ----

    _EMBED_BATCH_SIZE: int = 16

    async def embed(
        self,
        texts: list[str],
        dimensions: int = 1024,
    ) -> list[list[float]]:
        """文本向量化，分批调用 OpenAI Embeddings API。"""
        if not texts:
            return []

        client: AsyncOpenAI = self._get_client()
        model_id: str = self._get_model_id()
        results: list[list[float]] = []

        for i in range(0, len(texts), self._EMBED_BATCH_SIZE):
            batch: list[str] = texts[i : i + self._EMBED_BATCH_SIZE]
            resp = await client.embeddings.create(
                model=model_id,
                input=batch,
                dimensions=dimensions,
            )
            for d in resp.data:
                results.append(list(d.embedding))

        return results

    # ---- 核心 chat 方法 ----

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
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
            # 请求级状态：每次 chat() 新建，调用结束即释放，并发互不串线。
            state = _StreamParseState()
            parser: ReasoningStreamParser | None = (
                ReasoningStreamParser(self._policy)
                if self._policy.mode is ReasoningMode.TAGS
                else None
            )
            async for chunk in response:
                # 从 usage chunk 提取 token 统计（stream_options 开启时才返回）
                if getattr(chunk, "usage", None):
                    state.input_tokens = chunk.usage.prompt_tokens or 0
                    state.output_tokens = chunk.usage.completion_tokens or 0
                for sub in self._parse_stream_chunk(chunk, state, parser):
                    yield sub
            # 标签模式 flush 剩余缓冲（不展示协议标签）
            if parser is not None:
                for delta in parser.flush():
                    yield ChatChunk(text_deltas=(delta,))
            # 统一 yield 最终的结束包（携带 token 用量与结束原因，作为平行字段）
            yield ChatChunk(
                finish_reason=state.finish_reason,
                usage=TokenUsage(
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                ),
            )
        else:
            choice = response.choices[0]
            message = choice.message
            reasoning_content = getattr(message, "reasoning_content", None) or ""
            content = message.content or ""
            deltas: list[ChatTextDelta] = []
            if self._policy.mode is ReasoningMode.NATIVE:
                if reasoning_content:
                    deltas.append(ChatTextDelta(TextDeltaKind.REASONING, reasoning_content))
                if content:
                    deltas.append(ChatTextDelta(TextDeltaKind.RESPONSE, content))
            else:
                if content:
                    deltas.append(ChatTextDelta(TextDeltaKind.RESPONSE, content))
            usage = getattr(response, "usage", None)
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            yield ChatChunk(
                text_deltas=tuple(deltas),
                finish_reason="stop",
                usage=TokenUsage(input_tokens=in_tok, output_tokens=out_tok),
            )

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

    def _parse_stream_chunk(
        self,
        chunk,
        state: _StreamParseState,
        parser: ReasoningStreamParser | None,
    ) -> Iterator[ChatChunk]:
        """解析 OpenAI SSE chunk，按推理模式分离 reasoning/response 并累积工具调用。

        请求级状态由调用方传入的 state 承载，本方法不读写 Client 实例字段。
        多个完成的工具调用在结束包一次性写入 ChatChunk.tool_calls（平行字段）。
        """
        if not chunk.choices:
            return
        choice = chunk.choices[0]
        delta = choice.delta
        content = delta.content or ""
        reasoning_content = getattr(delta, "reasoning_content", None) or ""

        # 工具调用参数累积（跨 chunk 拼接）
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index if tc.index is not None else 0
                if idx not in state.pending_tool_calls:
                    state.pending_tool_calls[idx] = {
                        "id": "",
                        "name": "",
                        "arguments": "",
                    }
                pending = state.pending_tool_calls[idx]
                if tc.id:
                    pending["id"] = tc.id
                if tc.function and tc.function.name:
                    pending["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    pending["arguments"] += tc.function.arguments

        is_final = choice.finish_reason is not None
        if is_final:
            state.finish_reason = choice.finish_reason

        # 文本增量按模式分离（结束包与中间 chunk 同样处理，避免丢正文）
        text_deltas = self._extract_text_deltas(content, reasoning_content, parser)
        if text_deltas:
            yield ChatChunk(text_deltas=tuple(text_deltas))

        # 工具调用在结束包一次性写出（多个完成的工具调用一次写入，不逐条）
        if is_final:
            completed = [
                ToolCall(id=p["id"], name=p["name"], arguments=p["arguments"])
                for p in state.pending_tool_calls.values()
                if p["name"]
            ]
            if completed:
                yield ChatChunk(tool_calls=tuple(completed))

    def _extract_text_deltas(
        self,
        content: str,
        reasoning_content: str,
        parser: ReasoningStreamParser | None,
    ) -> list[ChatTextDelta]:
        """按推理模式将原始 content / reasoning_content 转为有序文本增量。"""
        if self._policy.mode is ReasoningMode.NATIVE:
            deltas: list[ChatTextDelta] = []
            if reasoning_content:
                deltas.append(ChatTextDelta(TextDeltaKind.REASONING, reasoning_content))
            if content:
                deltas.append(ChatTextDelta(TextDeltaKind.RESPONSE, content))
            return deltas
        if self._policy.mode is ReasoningMode.TAGS:
            # 标签模式只解析 content；reasoning_content 在 tags 模式下视为不存在
            if content and parser is not None:
                return list(parser.feed(content))
            return []
        # none：content 原样归为 response
        if content:
            return [ChatTextDelta(TextDeltaKind.RESPONSE, content)]
        return []
