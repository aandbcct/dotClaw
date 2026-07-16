"""TaskMessageBroker：同进程 Task 的唯一通信通道。

模块作用：维护每个 Task 的顺序消息流、端点消费游标和异步等待通知。
主功能：在一把异步锁内验证端点、追加消息、推进状态并唤醒等待者；因此每次
消息投递都是一个不可分割的内存原子操作。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import DefaultDict

from .task import (
    Task,
    TaskEndpoint,
    TaskEndpointBinding,
    TaskMessage,
    TaskMessageType,
    TaskStatus,
)


class TaskAccessError(PermissionError):
    """调用 Identity 或 Session 不属于 Task 指定端点。"""


class TaskStateError(RuntimeError):
    """消息类型不符合 Task 当前状态机。"""


@dataclass(frozen=True)
class TaskWaitResult:
    """wait_task 的稳定返回视图。"""

    task: Task
    messages: list[TaskMessage]
    timed_out: bool


class TaskMessageBroker:
    """以 Task 为粒度管理内存消息、游标和等待事件。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._messages: DefaultDict[str, list[TaskMessage]] = defaultdict(list)
        self._cursors: dict[tuple[str, TaskEndpoint], int] = {}
        self._conditions: DefaultDict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._lock: asyncio.Lock = asyncio.Lock()

    async def create_task(self, task: Task, source_run_id: str) -> None:
        """登记 Task 并原子写入首条 request 消息。"""
        async with self._lock:
            if task.task_id in self._tasks:
                raise TaskStateError(f"Task 已存在：{task.task_id}")
            self._tasks[task.task_id] = task
            self._cursors[(task.task_id, TaskEndpoint.SOURCE)] = 0
            self._cursors[(task.task_id, TaskEndpoint.TARGET)] = 0
            request: TaskMessage = self._append_message_locked(
                task=task,
                sender=TaskEndpoint.SOURCE,
                recipient=TaskEndpoint.TARGET,
                sender_run_id=source_run_id,
                message_type=TaskMessageType.REQUEST,
                payload=task.specification.render_user_message(),
            )
            # request 已由 Dispatcher 注入 target 的首条 user 消息，不应在其
            # 第一次 wait_task 时作为一条新的入站消息再次投递。
            self._cursors[(task.task_id, TaskEndpoint.TARGET)] = request.sequence

    async def mark_target_running(self, task_id: str) -> Task:
        """将已提交 Task 推进至 target 执行中。"""
        async with self._lock:
            task: Task = self._get_task_locked(task_id)
            if task.status is not TaskStatus.SUBMITTED:
                raise TaskStateError(f"Task 状态不能启动：{task.status.value}")
            task.status = TaskStatus.RUNNING_TARGET
            task.touch()
            return task

    async def send_message(
        self,
        task_id: str,
        sender: TaskEndpoint,
        identity_id: str,
        session_id: str,
        run_id: str,
        message_type: TaskMessageType,
        payload: str,
    ) -> TaskMessage:
        """校验端点和状态后追加一条普通或终态消息。"""
        async with self._lock:
            task: Task = self._get_task_locked(task_id)
            self._validate_endpoint_locked(task, sender, identity_id, session_id)
            recipient: TaskEndpoint = self._recipient_for(sender)
            self._validate_message_locked(task, sender, message_type)
            message: TaskMessage = self._append_message_locked(
                task, sender, recipient, run_id, message_type, payload,
            )
            self._apply_message_state_locked(task, message)
        await self._notify(task_id)
        return message

    async def cancel_task(
        self,
        task_id: str,
        identity_id: str,
        session_id: str,
        run_id: str,
        reason: str,
    ) -> Task:
        """由 source 原子写入 cancelled 消息并更新 Task 终态。"""
        async with self._lock:
            task: Task = self._get_task_locked(task_id)
            self._validate_endpoint_locked(task, TaskEndpoint.SOURCE, identity_id, session_id)
            if task.status.is_terminal():
                return task
            message: TaskMessage = self._append_message_locked(
                task, TaskEndpoint.SOURCE, TaskEndpoint.TARGET, run_id,
                TaskMessageType.CANCELLED, reason,
            )
            task.cancellation_requested = True
            self._apply_message_state_locked(task, message)
        await self._notify(task_id)
        return task

    async def wait_for_messages(
        self,
        task_id: str,
        endpoint: TaskEndpoint,
        identity_id: str,
        session_id: str,
        timeout: float | None,
    ) -> TaskWaitResult:
        """等待新消息或终态，并推进调用端的消费游标。"""
        await self._validate_access(task_id, endpoint, identity_id, session_id)
        available: list[TaskMessage] = await self._consume_available(task_id, endpoint)
        if available:
            task: Task = await self.get_task(task_id)
            return TaskWaitResult(task=task, messages=available, timed_out=False)
        task = await self.get_task(task_id)
        if task.status.is_terminal():
            return TaskWaitResult(task=task, messages=[], timed_out=False)
        condition: asyncio.Condition = self._conditions[task_id]
        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            task = await self.get_task(task_id)
            return TaskWaitResult(task=task, messages=[], timed_out=True)
        messages = await self._consume_available(task_id, endpoint)
        task = await self.get_task(task_id)
        return TaskWaitResult(task=task, messages=messages, timed_out=False)

    async def get_task(self, task_id: str) -> Task:
        """读取 Task 当前投影。"""
        async with self._lock:
            return self._get_task_locked(task_id)

    async def get_task_for_endpoint(
        self,
        task_id: str,
        endpoint: TaskEndpoint,
        identity_id: str,
        session_id: str,
    ) -> Task:
        """校验端点后读取状态，不改变任何消息消费游标。"""
        async with self._lock:
            task: Task = self._get_task_locked(task_id)
            self._validate_endpoint_locked(task, endpoint, identity_id, session_id)
            return task

    async def active_task_for_source(self, session_id: str) -> Task | None:
        """返回 source Session 的唯一活动 Task。"""
        async with self._lock:
            for task in self._tasks.values():
                if task.source.session_id == session_id and not task.status.is_terminal():
                    return task
        return None

    async def latest_task_for_source(self, session_id: str) -> Task | None:
        """返回 source Session 最近创建的 Task，含终态，供 Harness 隐式解析。"""
        async with self._lock:
            tasks: list[Task] = list(self._tasks.values())
            for task in reversed(tasks):
                if task.source.session_id == session_id:
                    return task
        return None

    async def _validate_access(self, task_id: str, endpoint: TaskEndpoint, identity_id: str, session_id: str) -> None:
        """在锁内校验调用端点。"""
        async with self._lock:
            task: Task = self._get_task_locked(task_id)
            self._validate_endpoint_locked(task, endpoint, identity_id, session_id)

    async def _consume_available(self, task_id: str, endpoint: TaskEndpoint) -> list[TaskMessage]:
        """读取并提交指定端点尚未消费的入站消息。"""
        async with self._lock:
            cursor_key: tuple[str, TaskEndpoint] = (task_id, endpoint)
            cursor: int = self._cursors[cursor_key]
            messages: list[TaskMessage] = [
                message for message in self._messages[task_id]
                if message.recipient is endpoint and message.sequence > cursor
            ]
            if messages:
                self._cursors[cursor_key] = messages[-1].sequence
            return messages

    def _get_task_locked(self, task_id: str) -> Task:
        """获取已登记 Task；调用方必须已持有锁。"""
        task: Task | None = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task 不存在：{task_id}")
        return task

    def _validate_endpoint_locked(self, task: Task, endpoint: TaskEndpoint, identity_id: str, session_id: str) -> None:
        """验证 Identity 与 Session 的双重端点绑定。"""
        binding: TaskEndpointBinding = task.binding_for(endpoint)
        if binding.identity_id != identity_id or binding.session_id != session_id:
            raise TaskAccessError("当前 Identity 或 Session 无权操作该 Task")

    def _validate_message_locked(self, task: Task, sender: TaskEndpoint, message_type: TaskMessageType) -> None:
        """验证消息在当前状态与端点下是否合法。"""
        if task.status.is_terminal():
            raise TaskStateError("Task 已终态，拒绝普通业务消息")
        allowed: set[tuple[TaskEndpoint, TaskMessageType, TaskStatus]] = {
            (TaskEndpoint.TARGET, TaskMessageType.PROGRESS, TaskStatus.RUNNING_TARGET),
            (TaskEndpoint.TARGET, TaskMessageType.QUESTION, TaskStatus.RUNNING_TARGET),
            (TaskEndpoint.TARGET, TaskMessageType.RESULT, TaskStatus.RUNNING_TARGET),
            (TaskEndpoint.TARGET, TaskMessageType.FAILED, TaskStatus.RUNNING_TARGET),
            (TaskEndpoint.SOURCE, TaskMessageType.REPLY, TaskStatus.WAITING_SOURCE),
            (TaskEndpoint.SOURCE, TaskMessageType.CONTEXT_UPDATE, TaskStatus.WAITING_SOURCE),
        }
        if (sender, message_type, task.status) not in allowed:
            raise TaskStateError(f"消息 {message_type.value} 不允许在 {task.status.value} 发送")

    def _append_message_locked(self, task: Task, sender: TaskEndpoint, recipient: TaskEndpoint, sender_run_id: str, message_type: TaskMessageType, payload: str) -> TaskMessage:
        """追加单条消息；调用方必须已经完成校验并持有锁。"""
        sequence: int = len(self._messages[task.task_id]) + 1
        message: TaskMessage = TaskMessage(
            task_id=task.task_id,
            sequence=sequence,
            sender=sender,
            recipient=recipient,
            sender_session_id=task.binding_for(sender).session_id,
            sender_run_id=sender_run_id,
            message_type=message_type,
            payload=payload,
            created_at=task.updated_at,
        )
        self._messages[task.task_id].append(message)
        return message

    def _apply_message_state_locked(self, task: Task, message: TaskMessage) -> None:
        """将消息事实投影为 Task 状态和终态结果。"""
        if message.message_type is TaskMessageType.QUESTION:
            task.status = TaskStatus.WAITING_SOURCE
        elif message.message_type in {TaskMessageType.REPLY, TaskMessageType.CONTEXT_UPDATE}:
            task.status = TaskStatus.RUNNING_TARGET
        elif message.message_type is TaskMessageType.RESULT:
            task.status = TaskStatus.COMPLETED
            task.result_message = message
        elif message.message_type is TaskMessageType.FAILED:
            task.status = TaskStatus.FAILED
            task.error = message.payload
            task.result_message = message
        elif message.message_type is TaskMessageType.CANCELLED:
            task.status = TaskStatus.CANCELLED
            task.error = message.payload
            task.result_message = message
        task.touch()

    async def _notify(self, task_id: str) -> None:
        """唤醒该 Task 上等待下一条消息的所有协程。"""
        condition: asyncio.Condition = self._conditions[task_id]
        async with condition:
            condition.notify_all()

    @staticmethod
    def _recipient_for(sender: TaskEndpoint) -> TaskEndpoint:
        """返回点对点消息的对端。"""
        return TaskEndpoint.TARGET if sender is TaskEndpoint.SOURCE else TaskEndpoint.SOURCE
