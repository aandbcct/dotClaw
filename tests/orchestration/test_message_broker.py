"""测试同进程 TaskMessageBroker 的状态机与端点隔离。"""

from __future__ import annotations

import asyncio

import pytest

from dotclaw.orchestration.message_broker import TaskAccessError, TaskMessageBroker, TaskStateError
from dotclaw.orchestration.task import Task, TaskEndpoint, TaskEndpointBinding, TaskMessageType, TaskSpecification, TaskStatus


def _task() -> Task:
    """创建一条固定绑定的测试 Task。"""
    return Task(
        task_id="task-1",
        specification=TaskSpecification(title="测试", objective="完成闭环"),
        source=TaskEndpointBinding(TaskEndpoint.SOURCE, "source", "source-session"),
        target=TaskEndpointBinding(TaskEndpoint.TARGET, "target", "target-session"),
    )


def test_question_reply_result_closes_state_machine() -> None:
    """目标提问、源回复、目标交付会按顺序驱动终态。"""
    async def scenario() -> None:
        broker: TaskMessageBroker = TaskMessageBroker()
        await broker.create_task(_task(), "source-run")
        await broker.mark_target_running("task-1")
        await broker.send_message("task-1", TaskEndpoint.TARGET, "target", "target-session", "target-run", TaskMessageType.QUESTION, "需要信息")
        assert (await broker.get_task("task-1")).status is TaskStatus.WAITING_SOURCE
        incoming = await broker.wait_for_messages("task-1", TaskEndpoint.SOURCE, "source", "source-session", 0.1)
        assert incoming.messages[0].message_type is TaskMessageType.QUESTION
        await broker.send_message("task-1", TaskEndpoint.SOURCE, "source", "source-session", "source-run", TaskMessageType.REPLY, "补充信息")
        await broker.send_message("task-1", TaskEndpoint.TARGET, "target", "target-session", "target-run", TaskMessageType.RESULT, "完成")
        task: Task = await broker.get_task("task-1")
        assert task.status is TaskStatus.COMPLETED
        assert task.result_message is not None
    asyncio.run(scenario())


def test_rejects_cross_session_access_and_post_terminal_messages() -> None:
    """同 Identity 的其他 Session 也不能操作 Task，终态拒绝普通消息。"""
    async def scenario() -> None:
        broker: TaskMessageBroker = TaskMessageBroker()
        await broker.create_task(_task(), "source-run")
        await broker.mark_target_running("task-1")
        with pytest.raises(TaskAccessError):
            await broker.wait_for_messages("task-1", TaskEndpoint.SOURCE, "source", "other-session", 0.0)
        await broker.send_message("task-1", TaskEndpoint.TARGET, "target", "target-session", "target-run", TaskMessageType.RESULT, "完成")
        with pytest.raises(TaskStateError):
            await broker.send_message("task-1", TaskEndpoint.TARGET, "target", "target-session", "target-run", TaskMessageType.PROGRESS, "迟到")
    asyncio.run(scenario())


def test_cancel_writes_terminal_message_and_state() -> None:
    """取消必须同时生成终态消息并更新 Task 状态。"""
    async def scenario() -> None:
        broker: TaskMessageBroker = TaskMessageBroker()
        await broker.create_task(_task(), "source-run")
        await broker.mark_target_running("task-1")
        task: Task = await broker.cancel_task("task-1", "source", "source-session", "source-run", "用户取消")
        assert task.status is TaskStatus.CANCELLED
        assert task.result_message is not None
        assert task.result_message.message_type is TaskMessageType.CANCELLED
    asyncio.run(scenario())


def test_target_does_not_receive_its_injected_request_again() -> None:
    """request 已进入 target 的首条 user 消息，不能在 wait 时重复投递。"""
    async def scenario() -> None:
        broker: TaskMessageBroker = TaskMessageBroker()
        await broker.create_task(_task(), "source-run")
        await broker.mark_target_running("task-1")
        result = await broker.wait_for_messages("task-1", TaskEndpoint.TARGET, "target", "target-session", 0.0)
        assert result.messages == []
        assert result.timed_out is True
    asyncio.run(scenario())
