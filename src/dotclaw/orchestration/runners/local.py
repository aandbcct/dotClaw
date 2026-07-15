"""本地 target Agent 运行器。

模块作用：在独立 Runtime、Agent 和 Session 中执行 target。运行器只维护本进程
协程句柄；Task 状态和双向消息仍由 Broker 单一负责。
"""

from __future__ import annotations

import asyncio

from ..task import Task, TaskEndpoint, TaskMessageType


class LocalAgentRunner:
    """启动、追踪并取消同进程 target Runtime。"""

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[None]] = {}

    def start(self, runtime: "Runtime", dispatcher: "AgentDispatcher", task: Task, session: "Session") -> None:
        """创建后台协程；source Run 会通过 wait_task 等待其终态。"""
        execution: asyncio.Task[None] = asyncio.create_task(self._run_target(runtime, dispatcher, task, session))
        self._running[task.task_id] = execution
        execution.add_done_callback(lambda _: self._running.pop(task.task_id, None))

    def cancel(self, task_id: str) -> None:
        """取消仍在运行的 target 协程。"""
        execution: asyncio.Task[None] | None = self._running.get(task_id)
        if execution is not None and not execution.done():
            execution.cancel()

    async def _run_target(self, runtime: "Runtime", dispatcher: "AgentDispatcher", task: Task, session: "Session") -> None:
        """完整装配 target Agent，并把异常转换成 failed 终态消息。"""
        try:
            target_identity = runtime.agent_registry.get(task.target.identity_id)
            if target_identity is None:
                raise RuntimeError(f"目标 Identity 不存在：{task.target.identity_id}")
            from ...agent.agent import Agent
            target_runtime: Runtime = runtime.derive(delegation_endpoint="target")
            target_agent: Agent = Agent(identity=target_identity, runtime=target_runtime, dispatcher=dispatcher)
            result: str = await target_agent.execute_in_session(target_runtime, session, task)
            await dispatcher.send_message(task.task_id, TaskEndpoint.TARGET, task.target.identity_id, task.target.session_id, "", TaskMessageType.RESULT, result)
        except asyncio.CancelledError:
            return
        except Exception as error:
            current: Task = await dispatcher.broker.get_task(task.task_id)
            if not current.status.is_terminal():
                await dispatcher.send_message(task.task_id, TaskEndpoint.TARGET, task.target.identity_id, task.target.session_id, "", TaskMessageType.FAILED, f"{type(error).__name__}: {error}")


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...runtime.runtime import Runtime
    from ...session.session import Session
    from ..dispatcher import AgentDispatcher
