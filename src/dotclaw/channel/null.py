"""NullChannel —— 黑洞 channel。

子 Agent 使用 NullChannel 避免 LLM 流式输出透传到用户终端。
所有输出方法均为 no-op，receive 直接返回空字符串。
"""

from __future__ import annotations

from .base import Channel


class NullChannel(Channel):
    """黑洞 channel —— 所有输入/输出均不产生副作用。

    用于子 Agent 隔离：子 Agent 的 LLM 输出不流到父 Agent 的用户终端，
    receive 不阻塞等待用户输入。
    """

    async def receive(self) -> str:
        """接收用户输入 —— 子 Agent 不应等待用户输入，直接返回空。"""
        return ""

    async def send(self, message: str) -> None:
        """发送消息 —— 不输出。"""

    async def stream(self, chunk: str) -> None:
        """流式输出 —— 不输出。"""

    async def ask_user(self, prompt: str) -> str:
        """向用户提问 —— 返回空（子 Agent 不应阻塞等待用户确认）。"""
        return ""

    def print_error(self, message: str) -> None:
        """打印错误 —— 不输出。"""

    def print_info(self, message: str) -> None:
        """打印信息 —— 不输出。"""

    async def print_markdown(self, md: str) -> None:
        """渲染 Markdown —— 不输出。"""
