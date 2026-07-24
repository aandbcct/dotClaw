"""将 Runtime 语义化增量事件适配为 Channel 输出。"""

from __future__ import annotations

from ..runtime.application.dto import LLMOutputEvent
from .base import Channel


class ChannelTextStreamAdapter:
    """将 Runtime 的模型增量事件转发到当前 Channel。"""

    def __init__(self, channel: Channel) -> None:
        """绑定入口层拥有的 Channel，不向 Runtime 泄漏具体通道类型。"""
        self._channel: Channel = channel

    async def emit(self, event: LLMOutputEvent) -> None:
        """转发非空增量内容；事件类型仅用于满足 Runtime 应用协议。"""
        if event.content:
            await self._channel.stream(event.content)
