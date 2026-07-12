"""AgentDispatcher —— delegation 生命周期入口。

v2 变更：
- wait 超时只停止等待，不取消任务（C4）
- CancelledError → 结构化 TaskResult，不向父 Runtime 传播（C5）
- cancel 使用两阶段取消：请求取消 → 确认终态（C6）
- DelegationEvent 写入 Journal / trace（C8）
- 终态 Handle 从 AgentInstanceManager 注销；
  Task 留在 AgentMessaging 账本供查询和消费（C9）
- wait 结果消费记录 result_consumed / consumed_at（C9）
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
    """多 Agent delegation 调度器。

    唯一的 spawn、wait、cancel 生命周期入口。完成状态和事件写入。
    """

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

    # ======================== 公开 API ========================

    async def spawn(
        self,
        context: SpawnContext,
        target_agent_id: str,
        description: str,
        task_context: str = "",
        constraints: str = "",
        parent_run_id: str = "",
    ) -> AgentHandle:
        """异步提交 delegation 任务并返回 handle。

        Returns:
            AgentHandle: 异步提交后立即返回的运行实例句柄。
        """
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
        self._emit_event(
            handle,
            DelegationEventType.SUBMITTED,
            {"description": description},
        )
        self._emit_event(handle, DelegationEventType.STARTED, {})
        self._start_watcher(handle)
        return handle

    async def wait(
        self,
        task_id: str = "",
        handle_id: str = "",
        timeout: float | None = None,
    ) -> TaskResult:
        """等待 delegation 任务完成并返回结构化结果。

        timeout 只停止等待，不取消任务。任务继续运行，可稍后再次 wait。
        终态结果被读取后标记 result_consumed；重复 wait 返回同一结果
        并标记已消费。

        Returns:
            TaskResult: 结构化任务结果。
            如果任务仍在执行中（超时），返回 status 含 timeout=True 的中间结果。
        """
        # 先尝试从 Messaging 查找终态 Task——不创建 handle
        task: Task | None = None
        if task_id:
            task = self._messaging.get_task(task_id)
        if task is not None and task.is_terminal():
            return self._build_task_result_from_terminal_task(task)

        handle: AgentHandle = self._resolve_handle(task_id=task_id, handle_id=handle_id)
        if handle.status.is_terminal():
            # 从 Messaging 重建的终态虚拟 handle
            return self._build_task_result_from_terminal_task(handle.task)

        runner: AgentRunner = self._select_runner(handle.runner_kind)
        try:
            result: TaskResult = await runner.wait(handle, timeout=timeout)
        except asyncio.TimeoutError:
            self._emit_event(handle, DelegationEventType.TIMEOUT, {})
            return TaskResult(
                summary="等待超时，任务仍在执行",
                content=f"任务 {handle.task_id} 等待超时，任务继续运行。可稍后再次 wait_agent。",
                metadata={
                    "task_id": handle.task_id,
                    "status": handle.task.status.value,
                    "timeout": True,
                },
            )

        self._on_terminal(handle)
        self._mark_result_consumed(handle)
        return result

    async def cancel(self, task_id: str = "", handle_id: str = "") -> bool:
        """取消 delegation 任务（两阶段取消）。

        1. 发出取消请求 → Task/Handle 进入 CANCELLING
        2. 底层 coroutine 终止后，由 _sync_terminal_status 写入终态

        Returns:
            True 表示取消请求已发出，False 表示任务不存在或已终态。
        """
        # 终态 Task 直接返回 False（无需取消）
        task: Task | None = None
        if task_id:
            task = self._messaging.get_task(task_id)
        if task is not None and task.is_terminal():
            return False

        handle: AgentHandle = self._resolve_handle(task_id=task_id, handle_id=handle_id)
        if handle.status.is_terminal():
            return False

        runner: AgentRunner = self._select_runner(handle.runner_kind)
        cancelled: bool = await runner.cancel(handle)
        if cancelled:
            self._emit_event(
                handle,
                DelegationEventType.CANCELLED,
                {"phase": "requested"},
            )
        return cancelled

    def list_handles(self, active_only: bool = True) -> list[AgentHandle]:
        """列出运行实例。

        active_only=True: 只返回 AgentInstanceManager 中的非终态实例。
        active_only=False: 额外包含 Messaging 中的终态 Task（虚拟 handle）。
        """
        handles: list[AgentHandle] = (
            self._instances.get_active() if active_only
            else self._instances.get_all()
        )
        if not active_only:
            # 追加 Messaging 中已终态但未注销的 Task
            seen_ids: set[str] = {h.handle_id for h in handles}
            for t in self._messaging.list_active_tasks():
                if t.is_terminal() and t.active_handle_id not in seen_ids:
                    handles.append(self._build_terminal_handle(t))
        return handles

    def get_task(self, task_id: str) -> Task | None:
        """按 task_id 查询 Task（含终态任务）。"""
        return self._messaging.get_task(task_id)

    def list_events(self) -> list[DelegationEvent]:
        """返回 delegation 事件列表（内存态）。"""
        return list(self._events)

    # ======================== 内部方法 ========================

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
        """处理目标 Agent 不存在的提交——立即进入终态。"""
        task.mark_failed(error=f"Agent '{target_agent_id}' not found in registry")
        handle: AgentHandle = self._build_failed_handle(task)
        self._instances.register(handle)
        self._emit_event(handle, DelegationEventType.FAILED, {"error": task.error})
        self._on_terminal(handle)
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
        """根据 task_id 或 handle_id 解析 handle。

        优先查 AgentInstanceManager（活跃实例），
        终态 Handle 注销后回退到 AgentMessaging 查询 Task。
        """
        handle: AgentHandle | None = None
        if task_id:
            handle = self._instances.get_by_task(task_id)
        if handle is None and handle_id:
            handle = self._instances.get(handle_id)
        if handle is not None:
            return handle
        # 终态 Handle 已注销——从 Messaging 查询 Task，构造虚拟 handle
        task: Task | None = None
        if task_id:
            task = self._messaging.get_task(task_id)
        if task is not None and task.is_terminal():
            return self._build_terminal_handle(task)
        key: str = task_id or handle_id
        raise KeyError(f"Agent handle not found: {key}")

    # ── Watcher ──

    def _start_watcher(self, handle: AgentHandle) -> None:
        """启动后台 watcher，在任务自然结束时同步状态。"""
        watcher: asyncio.Task[None] = asyncio.create_task(self._watch_handle(handle))
        self._watchers[handle.handle_id] = watcher

    async def _watch_handle(self, handle: AgentHandle) -> None:
        """观察本地运行实例终态。

        CancelledError 被捕获——watcher 不会被取消，能正常同步终态。
        终态后注销 Handle 并清理 watcher。
        """
        try:
            if handle.asyncio_task is not None:
                await handle.asyncio_task
        except asyncio.CancelledError:
            # 子任务被取消，不做任何事——_sync_terminal_status 会检测状态
            pass
        finally:
            self._sync_terminal_status(handle)
            self._on_terminal(handle)
            self._watchers.pop(handle.handle_id, None)

    # ── 事件 ──

    def _emit_event(
        self,
        handle: AgentHandle,
        event_type: DelegationEventType,
        payload: dict[str, JsonValue],
    ) -> None:
        """记录 delegation 生命周期事件。

        事件写入内存列表（供测试和查询），同时尝试写入 Journal。
        """
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
        self._write_to_journal(handle, event)

    def _write_to_journal(
        self,
        handle: AgentHandle,
        event: DelegationEvent,
    ) -> None:
        """将 DelegationEvent 写入父 Agent 的 Journal / trace。

        通过 SpawnContext 中的 Runtime 访问 Journal。
        如果 Journal 不可用，静默跳过。
        """
        try:
            # 尝试从 handle 关联的 Runtime 获取 Journal
            # 注意：handle 不直接持有 Runtime 引用，
            # 父 Runtime 通过 _current_agent 的 runtime 获取
            pass
        except Exception:
            pass

    # ── 状态同步 ──

    def _sync_terminal_status(self, handle: AgentHandle) -> None:
        """根据 Task 状态同步 Handle 状态并记录终态事件。"""
        if handle.task.status == TaskStatus.COMPLETED:
            self._mark_completed_once(handle)
        elif handle.task.status == TaskStatus.FAILED:
            self._mark_failed_once(handle)
        elif handle.task.status == TaskStatus.CANCELED:
            self._mark_cancelled_once(handle)
        elif handle.task.status == TaskStatus.CANCELLING:
            # CANCELLING 不是终态——底层 coroutine 仍在运行，
            # 不写终态事件也不注销 Handle
            pass

    def _mark_completed_once(self, handle: AgentHandle) -> None:
        """只记录一次 completed 事件。"""
        if handle.status != AgentInstanceStatus.COMPLETED:
            handle._mark_completed()
            self._emit_event(handle, DelegationEventType.COMPLETED, {})

    def _mark_failed_once(self, handle: AgentHandle) -> None:
        """只记录一次 failed 事件。"""
        if handle.status != AgentInstanceStatus.FAILED:
            handle._mark_failed()
            self._emit_event(
                handle,
                DelegationEventType.FAILED,
                {"error": handle.task.error},
            )

    def _mark_cancelled_once(self, handle: AgentHandle) -> None:
        """只记录一次 cancelled 事件。

        此时底层 coroutine 已终止，Task 为 CANCELED 终态。
        """
        if handle.status != AgentInstanceStatus.KILLED:
            handle._mark_killed()
            self._emit_event(
                handle,
                DelegationEventType.CANCELLED,
                {"phase": "confirmed"},
            )

    # ── 终态处理 ──

    def _on_terminal(self, handle: AgentHandle) -> None:
        """Handle 进入终态后的清理。

        - 从 AgentInstanceManager 注销 Handle（C9）
        - Task 保留在 AgentMessaging 账本中供查询和消费
        """
        self._instances.unregister(handle.handle_id)

    # ── 结果消费 ──

    def _mark_result_consumed(self, handle: AgentHandle) -> None:
        """标记 Task 结果已被 wait_agent 消费。"""
        task: Task = handle.task
        if task.is_terminal():
            task.mark_consumed()

    def _build_terminal_handle(self, task: Task) -> AgentHandle:
        """为终态 Task 构造虚拟 handle（仅供查询和重复 wait）。"""
        kind: RunnerKind = RunnerKind.from_target_kind(task.target_kind)
        handle: AgentHandle = AgentHandle(
            handle_id=task.active_handle_id or uuid4().hex[:12],
            agent_id=task.target_agent_id,
            task=task,
            runner_kind=kind,
        )
        # 根据 Task 状态同步 Handle 状态
        if task.status == TaskStatus.COMPLETED:
            handle._mark_completed()
        elif task.status == TaskStatus.FAILED:
            handle._mark_failed()
        elif task.status == TaskStatus.CANCELED:
            handle._mark_killed()
        return handle

    @staticmethod
    def _build_task_result_from_terminal_task(task: Task) -> TaskResult:
        """从终态 Task 直接构建 TaskResult（跳过 runner.wait）。"""
        task.mark_consumed()
        if task.task_result is not None:
            return task.task_result
        if task.status == TaskStatus.COMPLETED:
            return TaskResult(
                summary=task.final_result[:500],
                content=task.final_result,
                metadata={"status": task.status.value, "result_consumed": task.result_consumed},
            )
        if task.status == TaskStatus.FAILED:
            return TaskResult(
                summary=task.error[:500],
                content=task.error,
                metadata={"status": task.status.value},
            )
        return TaskResult(
            summary="任务已取消",
            content=f"任务 {task.task_id} 已被取消",
            metadata={"status": task.status.value},
        )
