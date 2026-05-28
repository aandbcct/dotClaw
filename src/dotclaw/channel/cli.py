"""CLI 通道实现"""

from __future__ import annotations

import asyncio
import sys
from typing import AsyncIterator

from rich.console import Console
from rich.markdown import Markdown

from .base import Channel


console = Console()


class CLIChannel(Channel):
    """
    命令行通道实现。

    使用 rich 库美化输出，支持流式渲染。
    """

    def __init__(self):
        self._input_lock = asyncio.Lock()
        self._pending_input: asyncio.Future[str] | None = None

    async def receive(self) -> str:
        """接收用户输入（同步 input 的 async 封装）"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, input, "\n>>> ")

    async def send(self, message: str) -> None:
        """发送消息给用户（打印）"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: console.print(message, end="")
        )

    async def stream(self, chunk: str) -> None:
        """流式输出一个 chunk（实时打印，不换行）"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: console.print(chunk, end="")
        )

    async def ask_user(self, prompt: str) -> str:
        """向用户提问"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(prompt))

    async def print_markdown(self, md: str) -> None:
        """渲染 Markdown"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: console.print(Markdown(md))
        )

    def print_error(self, message: str) -> None:
        """打印错误信息（红色）"""
        console.print(f"[red]{message}[/red]")

    def print_info(self, message: str) -> None:
        """打印普通信息"""
        console.print(message)
