"""Agent 核心循环"""

from __future__ import annotations


class AgentLoop:
    """
    Agent 主循环。

    负责：接收消息 → 构建 messages → 调用 LLM → 处理工具调用 → 返回结果
    """

    def __init__(self):
        self._running = False

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
        try:
            return "[dotClaw Phase 0] Agent Loop 骨架，待 Phase 1 实现 LLM 调用后即可运行"
        finally:
            self._running = False
