"""Shell 执行工具（builtin 子包 — Phase 5 迁移 + W1 修复）"""

from __future__ import annotations

import asyncio

from dotclaw.tools.handler import BuiltinToolHandler


async def exec_command(command: str) -> str:
    """
    执行 Shell 命令，返回标准输出。

    Phase 5 W1 修复：添加 CancelledError 处理。
    当 ToolExecutor 的 asyncio.wait_for 超时 cancel task 时，
    CancelledError（Python 3.9+ 继承 BaseException，不被 except Exception 捕获）
    必须先 kill 子进程再重新抛出，避免孤儿进程。
    """
    proc = None
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
        except asyncio.CancelledError:
            # Phase 5 W1 修复：ToolExecutor 超时 cancel → 必须 kill 子进程
            proc.kill()
            await proc.wait()
            raise  # 重新抛出，让 ToolExecutor 的 asyncio.wait_for 正常捕获
    except asyncio.CancelledError:
        # proc 创建阶段被 cancel（极端情况）
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise
    except Exception as e:
        return f"错误：{e}"


def get_exec_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
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
        handler_fn=exec_command,
        needs_approval=True,
        timeout=60.0,
    )
