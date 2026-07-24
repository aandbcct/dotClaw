"""LLM 客户端基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import AsyncIterator, Literal


class LLMUsage(StrEnum):
    """模型路由用途。"""
    CHAT = "chat"
    CONTEXT_COMPACTION = "context_compaction"


class TextDeltaKind(StrEnum):
    """流式文本增量的语义类别：模型推理过程或面向用户的响应。"""
    REASONING = "reasoning"
    RESPONSE = "response"


@dataclass(frozen=True)
class ChatTextDelta:
    """单次流式数据包内的有序文本增量，携带其语义类别。"""
    kind: TextDeltaKind
    content: str


@dataclass(frozen=True)
class TokenUsage:
    """一次 LLM 调用的 token 用量快照，与路由用途枚举 LLMUsage 互不混淆。"""
    input_tokens: int = 0
    output_tokens: int = 0


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


@dataclass(frozen=True)
class ChatChunk:
    """LLM 流式返回的一个标准化数据包。

    可同时携带有序文本增量、已组装工具调用、结束原因与 token 用量。
    text_deltas 为有序元组：原生 reasoning_content/content 同时存在时固定为
    reasoning 在前、response 在后；标签模式严格按原始文本顺序产生。
    """
    text_deltas: tuple[ChatTextDelta, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage | None = None


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

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        dimensions: int = 1024,
    ) -> list[list[float]]:
        """
        文本向量化。

        Parameters
        ----------
        texts : list[str]
            待向量化的文本列表
        dimensions : int
            输出向量维度，默认 1024

        Returns
        -------
        list[list[float]]
            每个输入文本对应的向量
        """
        ...
