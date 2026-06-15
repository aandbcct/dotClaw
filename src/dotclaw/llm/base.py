"""LLM 客户端基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal


@dataclass
class Message:
    """对话消息"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None  # assistant 消息中携带的工具调用列表


@dataclass
class ToolCall:
    """LLM 返回的函数调用"""
    id: str
    name: str
    arguments: str  # JSON 字符串


@dataclass
class ToolDefinition:
    """工具定义（对应 LLM 的 function schema）"""
    name: str
    description: str
    parameters: dict  # JSON Schema


@dataclass
class ChatChunk:
    """LLM 流式返回的一个 chunk"""
    content: str = ""
    tool_call: ToolCall | None = None
    is_final: bool = False  # 是否是最后一个 chunk
    finish_reason: str | None = None  # P13: "stop" / "tool_calls" / "length"
    input_tokens: int = 0   # 本次调用消耗的 prompt tokens（仅 is_final=True 的 chunk 携带）
    output_tokens: int = 0  # 本次调用产生的 completion tokens（同上）


class LLMClient(ABC):
    """LLM 客户端抽象基类"""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        """
        发送对话请求，返回流式 chunk 迭代器。

        Parameters
        ----------
        messages : list[Message]
            对话历史（不含 tool 调用结果，由调用方负责追加）
        tools : list[ToolDefinition] | None
            可用工具列表。None 表示纯文本对话。
        stream : bool
            是否流式返回。True = AsyncIterator[ChatChunk]；False = 等待完整回复再返回。

        Yields
        ------
        ChatChunk
            流式返回的文本片段或工具调用
        """
        ...
