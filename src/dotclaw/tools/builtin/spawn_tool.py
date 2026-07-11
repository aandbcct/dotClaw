"""delegation 内置工具 —— spawn_agent / wait_agent / list_agents。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from dotclaw.tools.handler import BuiltinToolHandler

if TYPE_CHECKING:
    from ...agent.agent import Agent
    from ...orchestration.handle import AgentHandle
    from ...orchestration.task import TaskResult


def get_spawn_agent_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 spawn_agent 工具 Handler。"""

    async def handle_spawn_agent(
        agent_id: str,
        description: str,
        context: str = "",
        constraints: str = "",
    ) -> str:
        """异步提交子 Agent 任务，立即返回本地索引。"""
        handle: AgentHandle = await agent.send(
            target_agent_id=agent_id,
            description=description,
            context=context,
            constraints=constraints,
        )
        payload: dict[str, str] = {
            "task_id": handle.task_id,
            "handle_id": handle.handle_id,
            "target_agent_id": handle.agent_id,
            "target_kind": handle.task.target_kind.value,
            "status": handle.task.status.value,
        }
        return json.dumps(payload, ensure_ascii=False)

    return BuiltinToolHandler(
        name="spawn_agent",
        description=(
            "把一个隔离上下文的任务委托给目标 Agent 异步执行。"
            "提交后立即返回 task_id、handle_id 和 status；"
            "需要结果时再调用 wait_agent(task_id)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "目标 Agent 的唯一标识。",
                },
                "description": {
                    "type": "string",
                    "description": "目标 Agent 要执行的任务描述。",
                },
                "context": {
                    "type": "string",
                    "description": "父 Agent 传递的必要上下文摘要。",
                },
                "constraints": {
                    "type": "string",
                    "description": "约束条件。",
                },
            },
            "required": ["agent_id", "description"],
        },
        handler_fn=handle_spawn_agent,
        needs_approval=False,
        timeout=10.0,
    )


def get_wait_agent_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 wait_agent 工具 Handler。"""

    async def handle_wait_agent(
        task_id: str = "",
        handle_id: str = "",
        timeout: float | None = None,
    ) -> str:
        """等待 delegation 任务完成并返回结构化结果。"""
        task_result: TaskResult = await agent.wait_task(
            task_id=task_id,
            handle_id=handle_id,
            timeout=timeout,
        )
        return json.dumps(task_result.to_dict(), ensure_ascii=False)

    return BuiltinToolHandler(
        name="wait_agent",
        description=(
            "等待 spawn_agent 提交的任务完成，并返回结构化 TaskResult。"
            "优先传 task_id；handle_id 仅用于调试或底层实例定位。"
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
                "timeout": {
                    "type": "number",
                    "description": "等待超时秒数，可选。",
                },
            },
        },
        handler_fn=handle_wait_agent,
        needs_approval=False,
        timeout=300.0,
    )


def get_list_agents_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 list_agents 工具 Handler。"""

    async def handle_list_agents() -> str:
        """列出当前活跃 delegation 实例。"""
        handles: list[AgentHandle] = agent.list_delegations()
        payload: list[dict[str, str]] = [
            {
                "task_id": handle.task_id,
                "handle_id": handle.handle_id,
                "target_agent_id": handle.agent_id,
                "target_kind": handle.task.target_kind.value,
                "status": handle.task.status.value,
                "description": handle.task.description,
            }
            for handle in handles
        ]
        return json.dumps(payload, ensure_ascii=False)

    return BuiltinToolHandler(
        name="list_agents",
        description="列出当前活跃的 delegation 任务和运行实例。",
        parameters={"type": "object", "properties": {}},
        handler_fn=handle_list_agents,
        needs_approval=False,
        timeout=5.0,
    )
