"""测试 AgentInstanceManager —— 运行时实例追踪。"""

import pytest

from dotclaw.orchestration.instance_manager import AgentInstanceManager
from dotclaw.orchestration.task import Task
from dotclaw.orchestration.handle import AgentHandle, AgentStatus


class TestAgentInstanceManager:
    """AgentInstanceManager 注册、查询、取消注册。"""

    @pytest.fixture
    def manager(self) -> AgentInstanceManager:
        return AgentInstanceManager()

    @pytest.fixture
    def handle(self) -> AgentHandle:
        task = Task(task_id="t1", requester="agent-a", description="x")
        return AgentHandle(handle_id="h1", agent_id="agent-b", task=task)

    def test_register_and_get(self, manager: AgentInstanceManager, handle: AgentHandle) -> None:
        manager.register(handle)
        assert manager.get("h1") is handle

    def test_get_nonexistent(self, manager: AgentInstanceManager) -> None:
        assert manager.get("ghost") is None

    def test_get_all(self, manager: AgentInstanceManager, handle: AgentHandle) -> None:
        h2 = AgentHandle(handle_id="h2", agent_id="agent-c",
                         task=Task(task_id="t2", requester="c", description="y"))
        manager.register(handle)
        manager.register(h2)
        all_handles = manager.get_all()
        assert len(all_handles) == 2

    def test_unregister(self, manager: AgentInstanceManager, handle: AgentHandle) -> None:
        manager.register(handle)
        manager.unregister("h1")
        assert manager.get("h1") is None

    def test_list_by_agent(self, manager: AgentInstanceManager, handle: AgentHandle) -> None:
        h2 = AgentHandle(handle_id="h2", agent_id="agent-b",
                         task=Task(task_id="t2", requester="b", description="z"))
        manager.register(handle)
        manager.register(h2)
        by_agent = manager.list_by_agent("agent-b")
        assert len(by_agent) == 2

    def test_get_active_non_terminal(self, manager: AgentInstanceManager, handle: AgentHandle) -> None:
        manager.register(handle)
        active = manager.get_active()
        assert len(active) == 1
        # 标记完成
        handle._mark_completed()
        active2 = manager.get_active()
        assert len(active2) == 0
