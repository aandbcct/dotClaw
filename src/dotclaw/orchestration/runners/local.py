"""LocalAgentRunner —— 本机子 Agent 执行器。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from ...agent.agent import Agent
from ..handle import AgentHandle, RunnerKind
from ..task import Task, TaskResult, TaskStatus
from .base import SpawnContext


class LocalAgentRunner:
    """本机 Agent runner。

    只负责本地执行细节：派生 Runtime、创建 child Agent、启动 asyncio.Task、
    等待结果和真实取消。
    """

    async def submit(self, task: Task, context: SpawnContext) -> AgentHandle:
        """提交本地任务并立即返回运行句柄。"""
        child_agent: Agent = self._create_child_agent(task, context)
        handle: AgentHandle = self._create_handle(task)
        run_task: asyncio.Task[Task] = asyncio.create_task(
            self._execute_child(child_agent, task, context)
        )
        handle.attach_asyncio_task(run_task)
        handle._mark_running()
        task.mark_working()
        return handle

    async def wait(self, handle: AgentHandle, timeout: float | None = None) -> TaskResult:
        """等待本地运行实例完成并返回结构化结果。"""
        if handle.asyncio_task is not None:
            await asyncio.wait_for(handle.asyncio_task, timeout=timeout)
        completed_task: Task = await handle.result(timeout=timeout)
        if completed_task.task_result is not None:
            return completed_task.task_result
        if completed_task.status == TaskStatus.FAILED:
            return TaskResult(
                summary=completed_task.error[:500],
                content=completed_task.error,
                metadata={"status": completed_task.status.value},
            )
        if completed_task.status == TaskStatus.CANCELED:
            return TaskResult(
                summary="任务已取消",
                content="任务已取消",
                metadata={"status": completed_task.status.value},
            )
        return TaskResult(summary=completed_task.final_result[:500], content=completed_task.final_result)

    async def cancel(self, handle: AgentHandle) -> bool:
        """真实取消本地 asyncio.Task。"""
        if handle.status.is_terminal():
            return False
        handle.cancel()
        return True

    def _create_child_agent(self, task: Task, context: SpawnContext) -> Agent:
        """创建隔离运行的子 Agent。"""
        target_identity = context.runtime.agent_registry.get(task.target_agent_id)
        if target_identity is None:
            raise RuntimeError(f"Agent '{task.target_agent_id}' not found in registry")
        child_runtime = context.runtime.derive()
        return Agent(identity=target_identity, runtime=child_runtime)

    def _create_handle(self, task: Task) -> AgentHandle:
        """创建本地运行实例句柄。"""
        return AgentHandle(
            handle_id=uuid4().hex[:12],
            agent_id=task.target_agent_id,
            task=task,
            runner_kind=RunnerKind.LOCAL,
        )

    async def _execute_child(
        self,
        child_agent: Agent,
        task: Task,
        context: SpawnContext,
    ) -> Task:
        """执行子 Agent，并把异常映射回 Task 状态。"""
        try:
            child_runtime = child_agent.runtime
            if child_runtime is None:
                raise RuntimeError("Child agent has no runtime")
            return await child_agent.execute(child_runtime, task)
        except asyncio.CancelledError:
            task.mark_canceled()
            raise
        except Exception as exc:
            task.mark_failed(error=f"{type(exc).__name__}: {exc}")
            return task

