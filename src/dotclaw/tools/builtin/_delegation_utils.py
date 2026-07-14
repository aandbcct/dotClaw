"""delegation 工具共享辅助函数。

避免 spawn_tool.py 和 kill_tool.py 重复代码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...agent.agent import Agent
    from ...tools.base import ToolExecutionContext


def resolve_agent(
    fallback_agent: "Agent",
    context: "ToolExecutionContext | None",
) -> "Agent":
    """从 context 解析当前 Agent。

    优先使用 context agent（支持嵌套 delegation 和正确的 agentrun_id），
    但当 context agent 没有 dispatcher 时 fallback 到工厂注入的顶层 agent。

    覆盖场景：
    - 顶层 Agent 调用 → context agent == 工厂 agent（有 dispatcher）
    - 子 Agent（无 dispatcher）调用 → fallback 到工厂 agent（有 dispatcher）
    - 子 Agent（有 dispatcher）调用 → 使用 context agent 支持嵌套 delegation
    """
    if context is not None and context.agent is not None:
        from ...agent.agent import Agent as AgentCls
        if isinstance(context.agent, AgentCls):
            ctx_agent: "Agent" = context.agent
            if ctx_agent._dispatcher is not None:
                return ctx_agent
    return fallback_agent


def resolve_parent_run_id(context: "ToolExecutionContext | None") -> str:
    """从 context 解析当前 AgentRun ID。"""
    if context is not None and context.agentrun_id:
        return context.agentrun_id
    return ""
