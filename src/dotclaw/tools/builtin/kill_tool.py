"""kill_agent 内置工具 —— 终止正在执行的子 Agent Task。

对标 A2A tasks/cancel。父 Agent 通过 tool_call 主动取消子 Agent 任务。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dotclaw.tools.handler import BuiltinToolHandler

if TYPE_CHECKING:
    from ...agent.agent import Agent


def get_kill_agent_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 kill_agent 工具 Handler。

    Args:
        agent: 当前 Agent 实例（发送方，持有 messaging）

    Returns:
        BuiltinToolHandler 实例
    """

    async def handle_kill_agent(task_id: str) -> str:
        """终止指定的子 Agent Task。

        Args:
            task_id: 要终止的 Task ID

        Returns:
            操作结果描述
        """
        if agent._messaging is None:  # type: ignore[union-attr]
            return "错误：Agent 未配置通信层"

        success: bool = agent._messaging.cancel(task_id)  # type: ignore[union-attr]
        if success:
            return f"已终止 Task {task_id}"
        return f"未找到 Task {task_id}"

    return BuiltinToolHandler(
        name="kill_agent",
        description=(
            "终止一个正在执行的子 Agent Task。"
            "参数 task_id 可从 spawn_agent 返回结果中获得。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "要终止的 Task ID",
                },
            },
            "required": ["task_id"],
        },
        handler_fn=handle_kill_agent,
        needs_approval=False,
        timeout=5.0,
    )
