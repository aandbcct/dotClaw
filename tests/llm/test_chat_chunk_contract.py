"""ChatChunk 公共数据契约测试（开发计划阶段一：冻结公共 LLM 数据契约）。

覆盖：空包、单文本 delta、同包 reasoning→response、多个工具调用、finish/usage-only 包。
解析器相关测试在阶段二新增，本文件只验证 LLM 公共数据包结构本身。
"""

from __future__ import annotations

from dataclasses import is_dataclass

from dotclaw.llm.base import (
    ChatChunk,
    ChatTextDelta,
    TextDeltaKind,
    TokenUsage,
    ToolCall,
)


def test_empty_chunk_has_default_empty_fields() -> None:
    """空包：所有字段使用安全的默认值，不携带任何增量或用量。"""
    chunk = ChatChunk()

    assert chunk.text_deltas == ()
    assert chunk.tool_calls == ()
    assert chunk.finish_reason is None
    assert chunk.usage is None


def test_single_text_delta_carries_response_content() -> None:
    """单文本 delta：一个 RESPONSE 增量按原样进入 text_deltas。"""
    chunk = ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "你好"),))

    assert len(chunk.text_deltas) == 1
    assert chunk.text_deltas[0].kind is TextDeltaKind.RESPONSE
    assert chunk.text_deltas[0].content == "你好"
    assert chunk.tool_calls == ()
    assert chunk.finish_reason is None
    assert chunk.usage is None


def test_same_packet_reasoning_then_response_keeps_order() -> None:
    """同包 reasoning→response：文本增量按原始顺序保留语义类别。"""
    chunk = ChatChunk(text_deltas=(
        ChatTextDelta(TextDeltaKind.REASONING, "让我想想"),
        ChatTextDelta(TextDeltaKind.RESPONSE, "结论"),
    ))

    assert chunk.text_deltas[0].kind is TextDeltaKind.REASONING
    assert chunk.text_deltas[1].kind is TextDeltaKind.RESPONSE
    assert [d.content for d in chunk.text_deltas] == ["让我想想", "结论"]


def test_multiple_tool_calls_bundled_in_one_packet() -> None:
    """多个工具调用：已组装的 ToolCall 以元组同包交付。"""
    chunk = ChatChunk(tool_calls=(
        ToolCall("call-1", "weather", '{"city":"上海"}'),
        ToolCall("call-2", "search", '{"q":"dotClaw"}'),
    ))

    assert len(chunk.tool_calls) == 2
    assert chunk.tool_calls[0].id == "call-1"
    assert chunk.tool_calls[1].name == "search"
    assert chunk.text_deltas == ()


def test_finish_and_usage_only_packet() -> None:
    """finish/usage-only 包：仅携带结束原因与 token 用量，无文本或工具。"""
    chunk = ChatChunk(finish_reason="stop", usage=TokenUsage(input_tokens=10, output_tokens=5))

    assert chunk.finish_reason == "stop"
    assert chunk.usage == TokenUsage(input_tokens=10, output_tokens=5)
    assert chunk.text_deltas == ()
    assert chunk.tool_calls == ()


def test_delta_and_usage_types_are_immutable() -> None:
    """辅助类型 ChatTextDelta / TokenUsage 为不可变值对象，可安全跨请求共享。"""
    assert is_dataclass(ChatTextDelta) and ChatTextDelta.__dataclass_params__.frozen
    assert is_dataclass(TokenUsage) and TokenUsage.__dataclass_params__.frozen
