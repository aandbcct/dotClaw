"""测试 AgentHandle —— Agent 实例的访问令牌。v2: 适配 CANCELLING 两阶段取消。"""

import asyncio
import uuid

import pytest

from dotclaw.orchestration.handle import AgentHandle, AgentInstanceStatus, AgentStatus
from dotclaw.orchestration.task import Task, TaskStatus


class TestAgentInstanceStatus:
    """AgentInstanceStatus 枚举。"""

    def test_all_statuses_defined(self) -> None:
        values: set[str] = {s.value for s in AgentInstanceStatus}
        assert values == {"idle", "running", "cancelling", "completed", "failed", "killed"}

    def test_is_terminal(self) -> None:
        assert AgentInstanceStatus.IDLE.is_terminal() is False
        assert AgentInstanceStatus.RUNNING.is_terminal() is False
        assert AgentInstanceStatus.CANCELLING.is_terminal() is False  # 不是终态
        assert AgentInstanceStatus.COMPLETED.is_terminal() is True
        assert AgentInstanceStatus.FAILED.is_terminal() is True
        assert AgentInstanceStatus.KILLED.is_terminal() is True


class TestAgentHandle:
    """AgentHandle 构造、状态、等待、取消。"""

    @pytest.fixture
    def task(self) -> Task:
        return Task(task_id="t1", requester="agent-a", description="test")

    @pytest.fixture
    def handle(self, task: Task) -> AgentHandle:
        return AgentHandle(handle_id="h1", agent_id="agent-b", task=task)

    def test_basic_construction(self, handle: AgentHandle, task: Task) -> None:
        assert handle.handle_id == "h1"
        assert handle.agent_id == "agent-b"
        assert handle.task is task

    def test_initial_status_is_idle(self, handle: AgentHandle) -> None:
        assert handle.status == AgentStatus.IDLE

    def test_mark_running(self, handle: AgentHandle) -> None:
        handle._mark_running()
        assert handle.status == AgentStatus.RUNNING

    def test_mark_cancelling(self, handle: AgentHandle) -> None:
        handle._mark_running()
        handle._mark_cancelling()
        assert handle.status == AgentStatus.CANCELLING

    def test_mark_completed_updates_from_task(self, handle: AgentHandle) -> None:
        handle.task.mark_completed(final_result="done")
        handle._mark_completed()
        assert handle.status == AgentStatus.COMPLETED

    def test_mark_failed_updates_from_task(self, handle: AgentHandle) -> None:
        handle.task.mark_failed(error="boom")
        handle._mark_failed()
        assert handle.status == AgentStatus.FAILED

    def test_mark_killed(self, handle: AgentHandle) -> None:
        handle._mark_killed()
        assert handle.status == AgentStatus.KILLED

    @pytest.mark.asyncio
    async def test_result_awaits_task_completion(self, handle: AgentHandle) -> None:
        async def _complete() -> None:
            await asyncio.sleep(0.01)
            handle._mark_running()
            handle.task.mark_completed(final_result="ok")
            handle._mark_completed()

        asyncio.create_task(_complete())
        result: Task = await handle.result()
        assert result.status == TaskStatus.COMPLETED
        assert result.final_result == "ok"

    @pytest.mark.asyncio
    async def test_result_timeout(self, handle: AgentHandle) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await handle.result(timeout=0.01)

    @pytest.mark.asyncio
    async def test_request_cancel_two_phase(self, handle: AgentHandle) -> None:
        """两阶段取消：request_cancel 进入 CANCELLING，coroutine 终止后进入 KILLED。"""
        handle._mark_running()
        handle.request_cancel()
        # 阶段 1：发出取消请求
        assert handle.status == AgentStatus.CANCELLING
        assert handle.task.status == TaskStatus.CANCELLING
        # 阶段 2：底层 coroutine 终止后
        handle.task.mark_canceled()
        handle._mark_killed()
        assert handle.status == AgentStatus.KILLED
        assert handle.task.status == TaskStatus.CANCELED

    def test_handle_id_is_unique(self) -> None:
        t = Task(task_id="ta", requester="a", description="x")
        h1 = AgentHandle(handle_id="id1", agent_id="a1", task=t)
        h2 = AgentHandle(handle_id="id2", agent_id="a2", task=t)
        assert h1.handle_id != h2.handle_id
