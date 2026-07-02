"""测试 NullChannel —— 黑洞 channel。"""

import pytest

from dotclaw.channel.null import NullChannel


class TestNullChannel:
    """NullChannel 各方法均无副作用。"""

    @pytest.mark.asyncio
    async def test_send_is_noop(self) -> None:
        """send 不抛异常，不输出。"""
        ch = NullChannel()
        await ch.send("hello")

    @pytest.mark.asyncio
    async def test_stream_is_noop(self) -> None:
        """stream 不抛异常。"""
        ch = NullChannel()
        await ch.stream("chunk")

    def test_print_info_is_noop(self) -> None:
        """print_info 不抛异常。"""
        ch = NullChannel()
        ch.print_info("info message")

    def test_print_error_is_noop(self) -> None:
        """print_error 不抛异常。"""
        ch = NullChannel()
        ch.print_error("error message")

    @pytest.mark.asyncio
    async def test_print_markdown_is_noop(self) -> None:
        """print_markdown 不抛异常。"""
        ch = NullChannel()
        await ch.print_markdown("# Title")

    @pytest.mark.asyncio
    async def test_ask_user_returns_empty(self) -> None:
        """ask_user 返回空字符串（子 Agent 不应等待用户输入）。"""
        ch = NullChannel()
        result = await ch.ask_user("question")
        assert result == ""

    @pytest.mark.asyncio
    async def test_receive_returns_empty(self) -> None:
        """receive 返回空字符串。"""
        ch = NullChannel()
        result = await ch.receive()
        assert result == ""
