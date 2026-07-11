"""AgentDispatcher —— delegation 生命周期入口。

Dispatcher 负责创建 Task、选择 Runner、注册 Handle、等待/取消任务和记录 delegation
事件。它不实现本地或远程执行细节，执行细节交给 AgentRunner。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from .events import DelegationEvent, DelegationEventType
from .handle import AgentHandle, AgentInstanceStatus, RunnerKind
from .instance_manager import AgentInstanceManager
from .messaging import AgentMessaging
from .runners.base import AgentRunner, SpawnContext
from .runners.local import LocalAgentRunner
from .task import JsonValue, Task, TaskResult, TaskStatus, TaskTargetKind


class AgentDispatcher:
    """多 Agent delegation 调度器。"""

    def __init__(
        self,
        messaging: AgentMessaging,
        instance_manager: AgentInstanceManager | None = None,
        local_runner: AgentRunner | None = None,
    ) -> None:
        self._messaging: AgentMessaging = messaging
        self._instances: AgentInstanceManager = instance_manager or AgentInstanceManager()
        self._runners: dict[RunnerKind, AgentRunner] = {
            RunnerKind.LOCAL: local_runner or LocalAgentRunner(),
        }
        self._events: list[DelegationEvent] = []
        self._watchers: dict[str, asyncio.Task[None]] = {}

    async def spawn(
        self,
        context: SpawnContext,
        target_agent_id: str,
        description: str,
        task_context: str = "",
        constraints: str = "",
        parent_run_id: str = "",
    ) -> AgentHandle:
        """异步提交 delegation 任务并返回 handle。"""
        task: Task = self._build_task(
            requester_agent_id=context.requester.agent_id,
            target_agent_id=target_agent_id,
            description=description,
            task_context=task_context,
            constraints=constraints,
            parent_run_id=parent_run_id or context.parent_run_id,
        )
        target = self._messaging.route(target_agent_id)
        if target is None:
            return self._spawn_failed_route(task, target_agent_id)

        self._messaging.track(task, target)
        runner: AgentRunner = self._select_runner(task.target_kind)
        handle: AgentHandle = await runner.submit(task, context)
        self._instances.register(handle)
        self._record_event(handle, DelegationEventType.SUBMITTED, {"description": description})
        self._record_event(handle, DelegationEventType.STARTED, {})
        self._start_watcher(handle)
        return handle

    async def wait(
        self,
        task_id: str = "",
        handle_id: str = "",
        timeout: float | None = None,
    ) -> TaskResult:
        """等待 delegation 任务完成并返回结构化结果。"""
        handle: AgentHandle = self._resolve_handle(task_id=task_id, handle_id=handle_id)
        runner: AgentRunner = self._select_runner(handle.runner_kind)
        try:
            result: TaskResult = await runner.wait(handle, timeout=timeout)
        except asyncio.TimeoutError:
            self._record_event(handle, DelegationEventType.TIMEOUT, {})
            raise
        self._sync_terminal_status(handle)
        return result

    async def cancel(self, task_id: str = "", handle_id: str = "") -> bool:
        """取消 delegation 任务。"""
        handle: AgentHandle = self._resolve_handle(task_id=task_id, handle_id=handle_id)
        runner: AgentRunner = self._select_runner(handle.runner_kind)
        cancelled: bool = await runner.cancel(handle)
        if cancelled:
            self._record_event(handle, DelegationEventType.CANCELLED, {})
        return cancelled

    def list_handles(self, active_only: bool = True) -> list[AgentHandle]:
        """列出运行实例。"""
        if active_only:
            return self._instances.get_active()
        return self._instances.get_all()

    def get_task(self, task_id: str) -> Task | None:
        """查询 Task。"""
        return self._messaging.get_task(task_id)

    def list_events(self) -> list[DelegationEvent]:
        """返回 delegation 事件列表。"""
        return list(self._events)

    def _build_task(
        self,
        requester_agent_id: str,
        target_agent_id: str,
        description: str,
        task_context: str,
        constraints: str,
        parent_run_id: str,
    ) -> Task:
        """创建本地 Task。"""
        return Task(
            task_id=uuid4().hex[:12],
            requester=requester_agent_id,
            target_agent_id=target_agent_id,
            target_kind=TaskTargetKind.LOCAL,
            description=description,
            context=task_context,
            constraints=constraints,
            parent_run_id=parent_run_id,
        )

    def _spawn_failed_route(self, task: Task, target_agent_id: str) -> AgentHandle:
        """处理目标 Agent 不存在的提交。"""
        task.mark_failed(error=f"Agent '{target_agent_id}' not found in registry")
        handle: AgentHandle = self._build_failed_handle(task)
        self._instances.register(handle)
        self._record_event(handle, DelegationEventType.FAILED, {"error": task.error})
        return handle

    def _build_failed_handle(self, task: Task) -> AgentHandle:
        """为失败 Task 创建失败 handle。"""
        handle: AgentHandle = AgentHandle(
            handle_id=uuid4().hex[:12],
            agent_id=task.target_agent_id,
            task=task,
            runner_kind=RunnerKind.from_target_kind(task.target_kind),
        )
        handle._mark_failed()
        return handle

    def _select_runner(self, runner_kind: RunnerKind | TaskTargetKind) -> AgentRunner:
        """按 runner 类型选择执行器。"""
        actual_kind: RunnerKind = (
            RunnerKind.from_target_kind(runner_kind)
            if isinstance(runner_kind, TaskTargetKind)
            else runner_kind
        )
        runner: AgentRunner | None = self._runners.get(actual_kind)
        if runner is None:
            raise RuntimeError(f"Runner '{actual_kind.value}' is not configured")
        return runner

    def _resolve_handle(self, task_id: str, handle_id: str) -> AgentHandle:
        """根据 task_id 或 handle_id 解析 handle。"""
        handle: AgentHandle | None = None
        if task_id:
            handle = self._instances.get_by_task(task_id)
        if handle is None and handle_id:
            handle = self._instances.get(handle_id)
        if handle is None:
            key: str = task_id or handle_id
            raise KeyError(f"Agent handle not found: {key}")
        return handle

    def _start_watcher(self, handle: AgentHandle) -> None:
        """启动后台 watcher，在任务自然结束时同步状态。"""
        watcher: asyncio.Task[None] = asyncio.create_task(self._watch_handle(handle))
        self._watchers[handle.handle_id] = watcher

    async def _watch_handle(self, handle: AgentHandle) -> None:
        """观察本地运行实例终态。"""
        try:
            if handle.asyncio_task is not None:
                await handle.asyncio_task
        except asyncio.CancelledError:
            handle._mark_killed()
        finally:
            self._sync_terminal_status(handle)
            self._watchers.pop(handle.handle_id, None)

    def _record_event(
        self,
        handle: AgentHandle,
        event_type: DelegationEventType,
        payload: dict[str, JsonValue],
    ) -> None:
        """记录 delegation 生命周期事件。"""
        event: DelegationEvent = DelegationEvent(
            task_id=handle.task_id,
            handle_id=handle.handle_id,
            parent_agent_id=handle.task.requester_agent_id,
            target_agent_id=handle.agent_id,
            target_kind=handle.task.target_kind,
            event_type=event_type,
            payload=payload,
        )
        self._events.append(event)

    def _sync_terminal_status(self, handle: AgentHandle) -> None:
        """根据 Task 终态同步 Handle 状态并记录事件。"""
        if handle.task.status == TaskStatus.COMPLETED:
            self._mark_completed_once(handle)
        elif handle.task.status == TaskStatus.FAILED:
            self._mark_failed_once(handle)
        elif handle.task.status == TaskStatus.CANCELED:
            self._mark_cancelled_once(handle)

    def _mark_completed_once(self, handle: AgentHandle) -> None:
        """只记录一次 completed。"""
        if handle.status != AgentInstanceStatus.COMPLETED:
            handle._mark_completed()
            self._record_event(handle, DelegationEventType.COMPLETED, {})

    def _mark_failed_once(self, handle: AgentHandle) -> None:
        """只记录一次 failed。"""
        if handle.status != AgentInstanceStatus.FAILED:
            handle._mark_failed()
            self._record_event(handle, DelegationEventType.FAILED, {"error": handle.task.error})

    def _mark_cancelled_once(self, handle: AgentHandle) -> None:
        """只记录一次 cancelled。"""
        if handle.status != AgentInstanceStatus.KILLED:
            handle._mark_killed()
            self._record_event(handle, DelegationEventType.CANCELLED, {})

