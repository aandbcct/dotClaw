"""Shell 命令工具；进程树所有权、超时和取消统一委托监管器。"""

from __future__ import annotations

from typing import Annotated

from dotclaw.tools.process_supervisor import ProcessSupervisor

from pydantic import Field

from dotclaw.tools.base import ToolContext
from dotclaw.tools.decorator import ToolPolicy, tool


@tool(
    name="builtin.process.execute",
    description="执行一条 Shell 命令。危险操作，执行前需用户确认。",
    policy=ToolPolicy.PROCESS,
    needs_approval=True,
    timeout=60.0,
)
async def execute(command: Annotated[str, Field(description="要执行的命令")], *, context: ToolContext) -> str:
    """执行命令，资源退出和超时统一交给本次调用的进程监管器。"""
    return await ProcessSupervisor().run(command, context.timeout)
