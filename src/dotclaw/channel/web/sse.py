"""把 Runtime 模型增量适配为单请求 SSE 流。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from enum import StrEnum

from ...runtime.application.dto import LLMOutputEvent
from ...runtime.application.ports import LLMOutputPort
from .schemas import SSEEvent, SSEEventType


class _StreamSignal(StrEnum):
    """SSE 内部队列的结束信号。"""

    END = "end"


def encode_sse_event(event: SSEEvent) -> str:
    """把事件编码为标准 SSE 的 event/data 两行格式。"""
    payload = json.dumps(
        event.data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event.event.value}\ndata: {payload}\n\n"


class SSEOutputAdapter(LLMOutputPort):
    """将单次 Run 的模型增量交给一个 SSE 订阅者。"""

    def __init__(self, queue_capacity: int = 64) -> None:
        """创建有界队列，避免慢订阅者导致无界内存增长。"""
        if queue_capacity <= 0:
            raise ValueError("SSE 队列容量必须大于 0")
        self._queue: asyncio.Queue[SSEEvent | _StreamSignal] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._disconnected: bool = False
        self._finished: bool = False

    async def emit(self, event: LLMOutputEvent) -> None:
        """按 Runtime 交付顺序发送非空模型增量。"""
        if not event.content or self._disconnected or self._finished:
            return
        await self._queue.put(
            SSEEvent(
                event=SSEEventType.MESSAGE_DELTA,
                data={
                    "session_id": event.session_id,
                    "run_id": event.run_id,
                    "kind": event.kind.value,
                    "content": event.content,
                },
            )
        )

    async def finish(self, event: SSEEvent) -> None:
        """发送唯一终态事件并关闭当前流。"""
        if self._finished:
            raise RuntimeError("SSE 消息流已存在终态事件")
        self._finished = True
        if self._disconnected:
            return
        await self._queue.put(event)
        await self._queue.put(_StreamSignal.END)

    def disconnect(self) -> None:
        """关闭订阅并丢弃积压输出，不向 Runtime 发送取消。"""
        if self._disconnected:
            return
        self._disconnected = True
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._queue.put_nowait(_StreamSignal.END)
        except asyncio.QueueFull:
            # 队列已在上面的排空循环中清空；仅防御并发写入造成的瞬时竞争。
            pass

    async def stream(self) -> AsyncIterator[str]:
        """依次产出已编码事件，终态或断连后结束。"""
        if self._disconnected:
            return
        while True:
            item = await self._queue.get()
            if item is _StreamSignal.END or self._disconnected:
                return
            yield encode_sse_event(item)
