"""Agent 核心循环"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..llm.base import Message
    from ..memory.store import Session
    from ..channel.base import Channel
    from ..config import Config
    from ..memory.store import SessionManager


class AgentLoop:
    """
    Agent 主循环。

    负责：接收消息 → 构建 messages → 调用 LLM → 处理工具调用 → 返回结果
    """

    def __init__(
        self,
        llm: "LLMProxy",
        session: "Session",
        session_mgr: "SessionManager",
        channel: "Channel",
        config: "Config",
    ):
        self.llm = llm
        self.session = session
        self.session_mgr = session_mgr
        self.channel = channel
        self.config = config
        self.model = config.llm.default_model
        self._running = False
        self._last_trace: dict | None = None

    async def run(self, user_message: str) -> str:
        """
        处理一条用户消息，返回 Agent 的回复。

        完整流程：
        1. 构建 messages (system + history + user)
        2. 调用 LLM
        3. 如果有 tool_calls → 执行工具 → 把结果追加到 messages → 回到 2
        4. 返回最终文本回复
        """
        self._running = True
        start_time = time.time()
        try:
            # Phase 1 LLM 调用实现后在这里写完整循环
            # 目前骨架阶段返回提示信息
            reply = (
                "[Phase 0] dotClaw v0.1.0 skeleton ready!\n"
                "AgentLoop.run() called. "
                "Implement LLM calling in Phase 1 to make this fully functional.\n"
                f"Session: [{self.session.id}] {self.session.title}"
            )
            await self.channel.send(reply)
            return reply
        finally:
            self._running = False
            elapsed_ms = int((time.time() - start_time) * 1000)
            self._last_trace = {
                "user_message": user_message,
                "duration_ms": elapsed_ms,
                "note": "skeleton - no LLM call yet",
            }

    def debug_trace(self, channel: "Channel"):
        """输出最近一次推理过程（供 /debug 命令调用）"""
        if self._last_trace:
            info = [
                "--- Last Inference Trace ---",
                f"User: {self._last_trace.get('user_message', '?')[:80]}",
                f"Duration: {self._last_trace.get('duration_ms', 0)}ms",
                f"Note: {self._last_trace.get('note', '')}",
                "---",
            ]
            channel.print_info("\n".join(info))
        else:
            channel.print_info("(no trace yet)")
