"""测试 AgentMessaging —— 纯通信层（路由 + 追踪 + 取消）。"""

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.agent.registry import AgentRegistry
from dotclaw.agent.messaging import AgentMessaging
from dotclaw.agent.task import Task, TaskStatus


class TestAgentMessaging:
    """AgentMessaging route / send / cancel / list_active。"""

    @pytest.fixture
    def registry(self) -> AgentRegistry:
        r = AgentRegistry()
        r.register(AgentIdentity(agent_id="researcher", agent_name="Researcher"))
        r.register(AgentIdentity(agent_id="coder", agent_name="Coder"))
        return r

    @pytest.fixture
    def messaging(self, registry: AgentRegistry) -> AgentMessaging:
        return AgentMessaging(registry=registry)

    def test_route_returns_identity(self, messaging: AgentMessaging) -> None:
        """route() 返回正确的 Identity。"""
        identity = messaging.route("researcher")
        assert identity is not None
        assert identity.agent_id == "researcher"
        assert identity.agent_name == "Researcher"

    def test_route_nonexistent_returns_none(self, messaging: AgentMessaging) -> None:
        """route() 对不存在的 agent 返回 None。"""
        assert messaging.route("ghost") is None

    def test_send_registers_task(self, messaging: AgentMessaging) -> None:
        """send() 将 Task 注册到活跃追踪表。"""
        identity = messaging.route("researcher")
        assert identity is not None

        task = Task(task_id="t1", requester="researcher", description="x")
        messaging.send(task, identity)

        active = messaging.list_active()
        assert len(active) == 1
        assert active[0].task_id == "t1"

    def test_send_multiple_tasks(self, messaging: AgentMessaging) -> None:
        """send() 多个 Task 全部注册。"""
        identity = messaging.route("coder")
        assert identity is not None

        t1 = Task(task_id="a", requester="coder", description="x")
        t2 = Task(task_id="b", requester="coder", description="y")
        messaging.send(t1, identity)
        messaging.send(t2, identity)

        assert len(messaging.list_active()) == 2

    def test_cancel_existing_task(self, messaging: AgentMessaging) -> None:
        """cancel() 成功取消已注册的 Task。"""
        identity = messaging.route("researcher")
        assert identity is not None

        task = Task(task_id="t2", requester="researcher", description="x")
        messaging.send(task, identity)

        result = messaging.cancel("t2")
        assert result is True
        assert task.status == TaskStatus.CANCELED

    def test_cancel_nonexistent_returns_false(self, messaging: AgentMessaging) -> None:
        """cancel() 对不存在的 task_id 返回 False。"""
        result = messaging.cancel("no-such-task")
        assert result is False

    def test_list_active_empty_initially(self, messaging: AgentMessaging) -> None:
        """初始时活跃 Task 列表为空。"""
        assert messaging.list_active() == []
