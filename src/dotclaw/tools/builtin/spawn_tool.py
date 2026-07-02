"""spawn_agent 内置工具 —— 父 Agent 派生子 Agent 执行子任务。

对标 A2A tasks/send：父 Agent 通过 tool_call 委托 Agent.send() 派生子 Agent。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dotclaw.tools.handler import BuiltinToolHandler

if TYPE_CHECKING:
    from ...agent.agent import Agent


def get_spawn_agent_handler(agent: "Agent") -> BuiltinToolHandler:
    """创建 spawn_agent 工具 Handler。

    通过闭包注入 Agent 实例，handler_fn 直接调用 agent.send()。

    Args:
        agent: 当前 Agent 实例（发送方）

    Returns:
        BuiltinToolHandler 实例
    """

    async def handle_spawn_agent(
        agent_id: str,
        description: str,
        context: str = "",
        constraints: str = "",
    ) -> str:
        """派生子 Agent 执行任务，等待完成后返回结果。

        Args:
            agent_id: 要派生的 Agent 的 agent_id
            description: 任务描述
            context: 父 Agent 提供的上下文摘要
            constraints: 约束条件

        Returns:
            子 Agent 的最终输出或错误信息
        """
        result = await agent.send(
            target_agent_id=agent_id,
            description=description,
            context=context,
            constraints=constraints,
        )
        if result.status.value == "completed":
            return result.final_result
        return f"[子Agent执行失败] {result.error or '未知错误'}"

    return BuiltinToolHandler(
        name="spawn_agent",
        description=(
            "派生一个子 Agent 来执行子任务。子 Agent 有独立的上下文和工具白名单，"
            "不会污染当前 Agent 的对话历史。适合需要隔离上下文的并行子任务。"
            "根据任务类型选择合适的 agent_id：代码任务用 code-engineer，"
            "数据分析用 data-analyst，写作/内容用 content-creator，"
            "专业知识问答用 domain-expert，客服/查询用 customer-service，"
            "任务规划/多步骤协调用 planner-coordinator。"
            "没有特别合适的就用 daily-assistant。"
            "返回子 Agent 的执行结果文本。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "要派生的 Agent 的唯一标识（agent_id）。"
                        "根据任务性质选择对应的 Agent。"
                        "可选值参考 system prompt 中的可用子 Agent 列表。"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "子任务的详细描述，子 Agent 将其作为用户输入执行。",
                },
                "context": {
                    "type": "string",
                    "description": "父 Agent 传递的必要上下文摘要（可选）。",
                },
                "constraints": {
                    "type": "string",
                    "description": "约束条件，如\"仅用内置工具\"、\"不访问网络\"（可选）。",
                },
            },
            "required": ["agent_id", "description"],
        },
        handler_fn=handle_spawn_agent,
        needs_approval=False,
        timeout=300.0,
    )
