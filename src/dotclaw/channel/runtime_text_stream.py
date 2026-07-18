"""将 Runtime 文本流协议适配为 Channel 输出。"""

from __future__ import annotations

from .base import Channel


class ChannelTextStreamAdapter:
    """将 Runtime 的模型文本增量转发到当前 Channel。"""

    def __init__(self, channel: Channel) -> None:
        """绑定入口层拥有的 Channel，不向 Runtime 泄漏具体通道类型。"""
        self._channel: Channel = channel

    async def emit(self, run_id: str, chunk: str) -> None:
        """转发非空文本块；运行标识仅用于满足应用协议。"""
        if chunk:
            await self._channel.stream(chunk)
