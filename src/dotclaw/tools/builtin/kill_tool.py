"""kill_agent 内置工具 —— 取消正在执行的 delegation 任务。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dotclaw.tools.handler import BuiltinToolHandler

if TYPE_CHECKING:
    from ...agent.agent import Agent


def get_kill_agent_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 kill_agent 工具 Handler。"""

    async def handle_kill_agent(task_id: str = "", handle_id: str = "") -> str:
        """按 task_id 优先取消任务，必要时可按 handle_id 取消实例。"""
        if not task_id and not handle_id:
            return "错误：必须提供 task_id 或 handle_id"
        success: bool = await agent.cancel_task(task_id=task_id, handle_id=handle_id)
        target: str = task_id or handle_id
        if success:
            return f"已请求取消 delegation {target}"
        return f"未能取消 delegation {target}"

    return BuiltinToolHandler(
        name="kill_agent",
        description=(
            "取消一个正在执行的 delegation 任务。本地任务会真实 cancel asyncio.Task；"
            "远程任务为 best-effort cancel。优先传 task_id。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "spawn_agent 返回的本地 task_id。",
                },
                "handle_id": {
                    "type": "string",
                    "description": "spawn_agent 返回的 handle_id，可选。",
                },
            },
        },
        handler_fn=handle_kill_agent,
        needs_approval=False,
        timeout=5.0,
    )
