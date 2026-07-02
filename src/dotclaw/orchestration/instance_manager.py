"""AgentInstanceManager —— 运行时 Agent 实例追踪。

追踪当前进程中所有活跃的 Agent 实例（通过 AgentHandle）。
不负责创建或销毁实例——那是 TaskDispatcher/Agent 的职责。
"""

from __future__ import annotations

from .handle import AgentHandle


class AgentInstanceManager:
    """运行时 Agent 实例管理器。

    每个 AgentHandle 代表一个运行中的 Agent 实例。
    Dispatcher 在创建实例时注册，实例终止时取消注册。
    """

    def __init__(self) -> None:
        self._instances: dict[str, AgentHandle] = {}

    # ── 注册/注销 ──

    def register(self, handle: AgentHandle) -> None:
        """注册一个 Agent 实例。

        Args:
            handle: Agent 实例的 Handle
        """
        self._instances[handle.handle_id] = handle

    def unregister(self, handle_id: str) -> None:
        """注销一个 Agent 实例。

        Args:
            handle_id: Handle ID
        """
        self._instances.pop(handle_id, None)

    # ── 查询 ──

    def get(self, handle_id: str) -> AgentHandle | None:
        """按 handle_id 查询。

        Args:
            handle_id: Handle ID

        Returns:
            AgentHandle，不存在则 None
        """
        return self._instances.get(handle_id)

    def get_all(self) -> list[AgentHandle]:
        """返回所有已注册实例。"""
        return list(self._instances.values())

    def get_active(self) -> list[AgentHandle]:
        """返回所有非终端状态的实例。"""
        return [h for h in self._instances.values() if not h.status.is_terminal()]

    def list_by_agent(self, agent_id: str) -> list[AgentHandle]:
        """按 agent_id 筛选实例。

        Args:
            agent_id: Agent Identity ID

        Returns:
            该 Agent 类型的实例列表
        """
        return [
            h for h in self._instances.values()
            if h.agent_id == agent_id
        ]
