"""kill_agent 内置工具 —— 取消正在执行的 delegation 任务。

v2: 从 ToolExecutionContext 解析当前 Agent，支持子 Agent 取消自己的 delegation。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dotclaw.tools.handler import BuiltinToolHandler

if TYPE_CHECKING:
    from ...agent.agent import Agent
    from ...tools.base import ToolExecutionContext


def _resolve_agent(agent: "Agent", context: "ToolExecutionContext | None") -> "Agent":
    """从 context 解析当前 Agent，fallback 到闭包绑定的 agent。"""
    if context is not None and context.agent is not None:
        from ...agent.agent import Agent as AgentCls
        if isinstance(context.agent, AgentCls):
            return context.agent
    return agent


def get_kill_agent_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 kill_agent 工具 Handler。

    kill_agent 执行两阶段取消：
    1. 请求取消 → Task/Handle 进入 CANCELLING
    2. 底层 coroutine 终止 → Dispatcher 写入终态 CANCELED/KILLED

    Args:
        agent: 工厂注入的 Agent，作为 context 不可用时的 fallback。
    """

    async def handle_kill_agent(
        task_id: str = "",
        handle_id: str = "",
        _context: "ToolExecutionContext | None" = None,
    ) -> str:
        """按 task_id 优先取消任务，必要时可按 handle_id 取消实例。"""
        if not task_id and not handle_id:
            return "错误：必须提供 task_id 或 handle_id"

        current_agent: Agent = _resolve_agent(agent, _context)
        success: bool = await current_agent.cancel_task(task_id=task_id, handle_id=handle_id)
        target: str = task_id or handle_id
        if success:
            return (
                f"已请求取消 delegation {target}。"
                "任务进入 CANCELLING 状态，底层 coroutine 终止后进入终态。"
            )
        return f"未能取消 delegation {target}（任务可能不存在或已终止）"

    return BuiltinToolHandler(
        name="kill_agent",
        description=(
            "取消一个正在执行的 delegation 任务。"
            "先请求取消（进入 CANCELLING 状态），"
            "底层 coroutine 实际终止后进入终态 CANCELED。"
            "本地任务会真实 cancel asyncio.Task。"
            "优先传 task_id。"
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
