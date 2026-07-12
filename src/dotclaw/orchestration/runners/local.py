"""LocalAgentRunner —— 本机子 Agent 执行器。

v2: 修复 wait 超时逻辑（超时只停止等待，不取消任务）和 CancelledError 处理
（转换为结构化 TaskResult，不向上传播）。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from ...agent.agent import Agent
from ..handle import AgentHandle, AgentInstanceStatus, RunnerKind
from ..task import Task, TaskResult, TaskStatus
from .base import AgentRunner, SpawnContext


class LocalAgentRunner:
    """本机 Agent runner。

    只负责本地执行细节：派生 Runtime、创建 child Agent、启动 asyncio.Task、
    非破坏性等待和取消请求。生命周期状态由 AgentDispatcher 统一管理。
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

    async def wait(
        self,
        handle: AgentHandle,
        timeout: float | None = None,
    ) -> TaskResult:
        """等待本地运行实例完成并返回结构化结果。

        timeout 只停止等待，不取消底层 asyncio.Task。
        超时后调用方可以稍后再次 wait。
        """
        try:
            await handle.result(timeout=timeout)
        except asyncio.TimeoutError:
            # 超时只停止等待，任务继续运行
            return TaskResult(
                summary="任务仍在执行中，尚未完成",
                content=f"任务 {handle.task_id} 仍在执行中，请稍后再次 wait_agent 获取结果",
                metadata={
                    "status": handle.task.status.value,
                    "task_id": handle.task_id,
                    "timeout": True,
                },
            )
        return self._build_task_result(handle.task)

    async def cancel(self, handle: AgentHandle) -> bool:
        """请求取消本地 asyncio.Task（不立即写终态）。

        Returns:
            True 表示取消请求已发出（或任务已终态），False 表示无法操作。
        """
        if handle.status.is_terminal():
            return False
        handle.request_cancel()
        return True

    # ── 内部方法 ──

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
        """执行子 Agent，异常映射回 Task 状态。

        CancelledError 不再向上传播——转换为结构化 TaskResult(status=canceled)
        并标记 Task 为 CANCELED 终态。
        """
        try:
            child_runtime = child_agent.runtime
            if child_runtime is None:
                raise RuntimeError("Child agent has no runtime")
            return await child_agent.execute(child_runtime, task)
        except asyncio.CancelledError:
            task.mark_canceled()
            return task
        except Exception as exc:
            task.mark_failed(error=f"{type(exc).__name__}: {exc}")
            return task

    @staticmethod
    def _build_task_result(task: Task) -> TaskResult:
        """从 Task 状态构建结构化 TaskResult。"""
        if task.task_result is not None:
            return task.task_result
        if task.status == TaskStatus.COMPLETED:
            return TaskResult(
                summary=task.final_result[:500],
                content=task.final_result,
            )
        if task.status == TaskStatus.FAILED:
            return TaskResult(
                summary=task.error[:500],
                content=task.error,
                metadata={"status": task.status.value},
            )
        if task.status in (TaskStatus.CANCELED, TaskStatus.CANCELLING):
            return TaskResult(
                summary="任务已取消",
                content=f"任务 {task.task_id} 已被取消",
                metadata={"status": task.status.value},
            )
        return TaskResult(
            summary=task.final_result[:500],
            content=task.final_result,
            metadata={"status": task.status.value},
        )
