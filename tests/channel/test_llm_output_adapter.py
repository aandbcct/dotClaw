"""阶段五：ChannelLLMOutputAdapter 语义分区展示契约测试。

覆盖开发计划阶段五「测试与门槛」：response-only、reasoning-only、
reasoning→response、response→reasoning、连续同类不重复、空文本、多次 LLM
调用、不同 run_id 独立标题、并发不串流。适配器按 run_id 记忆上次展示语义，
切换时打印一次「思考」/「回答」标题，模型文本经纯文本路径输出。
"""

from __future__ import annotations

import asyncio

import pytest

from dotclaw.channel.base import Channel
from dotclaw.channel.runtime_llm_output import ChannelLLMOutputAdapter
from dotclaw.runtime.application.dto import LLMOutputEvent, LLMOutputKind


class CollectingChannel(Channel):
    """记录所有 stream 输出的收集型通道，不依赖真实终端。"""

    def __init__(self) -> None:
        """初始化收集到的文本块列表。"""
        self.chunks: list[str] = []

    async def receive(self) -> str:
        """本测试不读取用户输入。"""
        return ""

    async def send(self, message: str) -> None:
        """本测试不使用非流式发送。"""
        pass

    async def stream(self, chunk: str) -> None:
        """记录转发的文本块。"""
        self.chunks.append(chunk)

    async def ask_user(self, prompt: str) -> str:
        """本测试不触发交互式审批。"""
        return ""


def _event(run_id: str, kind: LLMOutputKind, content: str) -> LLMOutputEvent:
    """构造最小增量事件；session_id 对适配器逻辑无影响，填占位值。"""
    return LLMOutputEvent(session_id="s", run_id=run_id, kind=kind, content=content)


@pytest.mark.asyncio
async def test_response_only_prints_single_answer_header() -> None:
    """response-only：首个 response 前打印一次「回答：」，连续同类不重复。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "你好"))
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "世界"))
    assert ch.chunks == ["\n回答：\n", "你好", "世界"]
    assert ch.chunks.count("\n回答：\n") == 1


@pytest.mark.asyncio
async def test_reasoning_only_prints_single_think_header() -> None:
    """reasoning-only：首个 reasoning 前打印一次「思考：」，连续同类不重复。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.REASONING_DELTA, "让我想想"))
    await adapter.emit(_event("r1", LLMOutputKind.REASONING_DELTA, "再想想"))
    assert ch.chunks == ["\n思考：\n", "让我想想", "再想想"]
    assert ch.chunks.count("\n思考：\n") == 1


@pytest.mark.asyncio
async def test_reasoning_then_response_prints_both_headers_once() -> None:
    """reasoning→response：两段各打印一次标题，顺序正确。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.REASONING_DELTA, "想"))
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "答"))
    assert ch.chunks == ["\n思考：\n", "想", "\n回答：\n", "答"]


@pytest.mark.asyncio
async def test_response_then_reasoning_prints_both_headers() -> None:
    """response→reasoning：已展示 response 后再 reasoning，重打「思考：」。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "答"))
    await adapter.emit(_event("r1", LLMOutputKind.REASONING_DELTA, "想"))
    assert ch.chunks == ["\n回答：\n", "答", "\n思考：\n", "想"]


@pytest.mark.asyncio
async def test_consecutive_same_kind_no_repeated_header() -> None:
    """连续同类增量不重复标题。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "a"))
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "b"))
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "c"))
    assert ch.chunks == ["\n回答：\n", "a", "b", "c"]


@pytest.mark.asyncio
async def test_empty_content_skipped_no_header() -> None:
    """空文本不展示、不打标题；后续非空同类增量仍在首次出现时打印标题。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, ""))
    assert ch.chunks == []
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "x"))
    assert ch.chunks == ["\n回答：\n", "x"]


@pytest.mark.asyncio
async def test_multiple_llm_calls_same_run_reuse_header_state() -> None:
    """同一 Run 多次 LLM 调用：kind 跨调用按 run_id 记忆，再次切换会重打标题。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.REASONING_DELTA, "想1"))
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "答1"))
    # 第二次 LLM 调用以 reasoning 重新开头：相对上次 response 切换，重打「思考：」。
    await adapter.emit(_event("r1", LLMOutputKind.REASONING_DELTA, "想2"))
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "答2"))
    assert ch.chunks == [
        "\n思考：\n", "想1",
        "\n回答：\n", "答1",
        "\n思考：\n", "想2",
        "\n回答：\n", "答2",
    ]


@pytest.mark.asyncio
async def test_distinct_run_ids_independent_headers() -> None:
    """不同 run_id 独立记忆标题，互不干扰。"""
    ch = CollectingChannel()
    adapter = ChannelLLMOutputAdapter(ch)
    await adapter.emit(_event("r1", LLMOutputKind.RESPONSE_DELTA, "A"))
    await adapter.emit(_event("r2", LLMOutputKind.RESPONSE_DELTA, "B"))
    assert ch.chunks == ["\n回答：\n", "A", "\n回答：\n", "B"]


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_cross_stream() -> None:
    """同一适配器实例服务两个并发 Run，按 run_id 隔离，不串流。"""
    ch_a = CollectingChannel()
    ch_b = CollectingChannel()
    adapter_a = ChannelLLMOutputAdapter(ch_a)
    adapter_b = ChannelLLMOutputAdapter(ch_b)
    await asyncio.gather(
        adapter_a.emit(_event("ra", LLMOutputKind.RESPONSE_DELTA, "a1")),
        adapter_b.emit(_event("rb", LLMOutputKind.RESPONSE_DELTA, "b1")),
        adapter_a.emit(_event("ra", LLMOutputKind.RESPONSE_DELTA, "a2")),
        adapter_b.emit(_event("rb", LLMOutputKind.RESPONSE_DELTA, "b2")),
    )
    assert ch_a.chunks == ["\n回答：\n", "a1", "a2"]
    assert ch_b.chunks == ["\n回答：\n", "b1", "b2"]
