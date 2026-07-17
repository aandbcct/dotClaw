"""AgentDispatcher：delegation 的本地门面。

模块作用：把工具层请求编排为创建 target Session、登记 Task、启动本地 runner
和取消运行句柄四个步骤。它不保存消息账本，所有通信状态均委托给 Broker。
"""

from __future__ import annotations

from uuid import uuid4

from .message_broker import TaskMessageBroker, TaskWaitResult
from .runners.local import LocalAgentRunner
from .task import (
    Task,
    TaskEndpoint,
    TaskEndpointBinding,
    TaskMessage,
    TaskMessageType,
    TaskSpecification,
)


class AgentDispatcher:
    """协调本进程内单层 delegation 的公开服务。"""

    def __init__(self, broker: TaskMessageBroker, runner: LocalAgentRunner | None = None) -> None:
        self._broker: TaskMessageBroker = broker
        self._runner: LocalAgentRunner = runner or LocalAgentRunner()

    @property
    def broker(self) -> TaskMessageBroker:
        """暴露 Broker 供 Runtime 进行活动 Task 收口检查。"""
        return self._broker

    async def delegate(
        self,
        runtime: "Runtime",
        source_identity_id: str,
        source_session_id: str,
        source_run_id: str,
        target_identity_id: str,
        specification: TaskSpecification,
    ) -> Task:
        """创建 target Session、Task 与运行协程，拒绝第二个活动 Task。"""
        target_identity = runtime.agent_registry.get(target_identity_id)
        if target_identity is None:
            raise KeyError(f"目标 Identity 不存在：{target_identity_id}")
        required_tools: set[str] = {"task_send_message", "wait_task"}
        if target_identity.allowed_tools and not required_tools.issubset(set(target_identity.allowed_tools)):
            raise RuntimeError("目标 Identity 的 allowed_tools 必须包含 task_send_message 与 wait_task")
        target_session = await runtime.session_mgr.create(
            title=f"委托-{specification.title}",
            model=target_identity.resolve_model(runtime.config.llm.default_model) if runtime.config is not None else target_identity.model,
            agent_id=target_identity.agent_id,
        )
        task: Task = await self.start_v2_delegation(
            source_identity_id,
            source_session_id,
            source_run_id,
            target_identity_id,
            target_session.id,
            specification,
        )
        self._runner.start(runtime, self, task, target_session)
        return task

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
        await self.send_message(
            task_id,
            TaskEndpoint.TARGET,
            task.target.identity_id,
            task.target.session_id,
            target_run_id,
            message_type,
            output,
        )
        return await self._broker.get_task(task_id)

    async def send_message(
        self,
        task_id: str,
        endpoint: TaskEndpoint,
        identity_id: str,
        session_id: str,
        run_id: str,
        message_type: TaskMessageType,
        payload: str,
    ) -> TaskMessage:
        """转发经端点绑定验证的业务消息。"""
        return await self._broker.send_message(task_id, endpoint, identity_id, session_id, run_id, message_type, payload)

    async def wait_task(
        self,
        task_id: str,
        endpoint: TaskEndpoint,
        identity_id: str,
        session_id: str,
        timeout: float | None,
    ) -> TaskWaitResult:
        """等待当前端点的新入站消息或 Task 终态。"""
        return await self._broker.wait_for_messages(task_id, endpoint, identity_id, session_id, timeout)

    async def task_status(self, task_id: str, endpoint: TaskEndpoint, identity_id: str, session_id: str) -> Task:
        """返回已完成端点鉴权的 Task 状态投影。"""
        return await self._broker.get_task_for_endpoint(task_id, endpoint, identity_id, session_id)

    async def cancel_task(self, task_id: str, identity_id: str, session_id: str, run_id: str, reason: str) -> Task:
        """先写入取消终态，再停止 target 的本地协程。"""
        task: Task = await self._broker.cancel_task(task_id, identity_id, session_id, run_id, reason)
        self._runner.cancel(task_id)
        return task


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime
