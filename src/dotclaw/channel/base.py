"""Channel 抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod


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

    @abstractmethod
    async def select_model(
        self,
        current_model: str,
        available_models: tuple[str, ...],
    ) -> str | None:
        """展示模型列表并返回用户选择；取消或非法输入返回 None。"""
        ...

    def print_error(self, message: str) -> None:
        """打印错误信息（默认实现：打印到 stderr）"""
        import sys
        print(f"错误：{message}", file=sys.stderr)

    def print_info(self, message: str) -> None:
        """打印普通信息（默认实现：打印到 stdout）"""
        print(message)

    async def print_markdown(self, md: str) -> None:
        """渲染 Markdown（默认实现：直接打印）"""
        print(md)
