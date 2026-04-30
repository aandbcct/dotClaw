"""Shell 执行工具"""

from __future__ import annotations

import asyncio

from .base import ToolResult, register_tool


@register_tool(
    name="exec",
    description="执行一条 Shell 命令。危险操作，执行前需用户确认。",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令",
            }
        },
        "required": ["command"],
    },
)
async def exec_command(command: str) -> str:
    """
    执行 Shell 命令，返回标准输出。
    超时 60 秒。
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode("utf-8", errors="replace")
            return output if output else "(命令无输出)"
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "错误：命令执行超时（60秒）"
    except Exception as e:
        return f"错误：{e}"
