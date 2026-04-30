"""Channel 抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class Channel(ABC):
    """消息通道抽象基类"""

    @abstractmethod
    async def receive(self) -> str:
        """接收用户输入"""
        ...

    @abstractmethod
    async def send(self, message: str) -> None:
        """发送消息给用户"""
        ...

    @abstractmethod
    async def stream(self, chunk: str) -> None:
        """流式输出一个 chunk"""
        ...

    @abstractmethod
    async def ask_user(self, prompt: str) -> str:
        """向用户提问（用于审批等）"""
        ...
