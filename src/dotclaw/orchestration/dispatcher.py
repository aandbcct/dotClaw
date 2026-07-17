"""Runtime v2 delegation 的 Task 状态机门面。

模块作用：为 RuntimeDelegationAdapter 保存 Task / Broker 的编排事实。
子 Run 的实际创建、执行和取消由适配器及 SessionRunCoordinator 处理，本模块
不依赖 Runtime、Journal 或本地 runner。
"""

from __future__ import annotations

from uuid import uuid4

from .message_broker import TaskMessageBroker
from .task import (
    Task,
    TaskEndpoint,
    TaskEndpointBinding,
    TaskMessageType,
    TaskSpecification,
)


class AgentDispatcher:
    """协调 Runtime v2 delegation 的 Task 状态投影。"""

    def __init__(self, broker: TaskMessageBroker) -> None:
        self._broker: TaskMessageBroker = broker

    @property
    def broker(self) -> TaskMessageBroker:
        """暴露 Broker 供 RuntimeDelegationAdapter 完成状态投影。"""
        return self._broker

    async def start_v2_delegation(
        self,
        source_identity_id: str,
        source_session_id: str,
        source_run_id: str,
        target_identity_id: str,
        target_session_id: str,
        specification: TaskSpecification,
    ) -> Task:
        """登记 Runtime v2 子运行对应的 Task，但不启动旧 Runtime runner。

        Task 的消息状态机仍由 Dispatcher/Broker 持有；实际子运行改由
        RuntimeDelegationAdapter 经 SessionRunCoordinator 执行。
        """
        active_task: Task | None = await self._broker.active_task_for_source(source_session_id)
        if active_task is not None:
            raise RuntimeError(f"当前 Session 已有活动 Task：{active_task.task_id}")
        task: Task = Task(
            task_id=uuid4().hex,
            specification=specification,
            source=TaskEndpointBinding(TaskEndpoint.SOURCE, source_identity_id, source_session_id),
            target=TaskEndpointBinding(TaskEndpoint.TARGET, target_identity_id, target_session_id),
        )
        await self._broker.create_task(task, source_run_id)
        return await self._broker.mark_target_running(task.task_id)

    async def finish_v2_delegation(
        self,
        task_id: str,
        target_run_id: str,
        output: str,
        succeeded: bool,
    ) -> Task:
        """将 v2 子 Run 终态回调投影到既有 Task 状态机。"""
        task: Task = await self._broker.get_task(task_id)
        if task.status.is_terminal():
            return task
        message_type: TaskMessageType = TaskMessageType.RESULT if succeeded else TaskMessageType.FAILED
        await self._broker.send_message(
            task_id,
            TaskEndpoint.TARGET,
            task.target.identity_id,
            task.target.session_id,
            target_run_id,
            message_type,
            output,
        )
        return await self._broker.get_task(task_id)

    async def cancel_task(self, task_id: str, identity_id: str, session_id: str, run_id: str, reason: str) -> Task:
        """写入取消终态；实际子 Run 取消由适配器转交协调器。"""
        return await self._broker.cancel_task(task_id, identity_id, session_id, run_id, reason)
