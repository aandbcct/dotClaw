"""AgentInstanceManager —— 运行时 Agent 实例索引。

只负责保存和查询 AgentHandle，不创建 Agent、不执行任务、不直接取消 coroutine。
真实生命周期由 AgentDispatcher 和 AgentRunner 管理。
"""

from __future__ import annotations

from .handle import AgentHandle


class AgentInstanceManager:
    """运行时 Agent 实例管理器。"""

    def __init__(self) -> None:
        self._instances: dict[str, AgentHandle] = {}
        self._task_index: dict[str, str] = {}

    # ── 注册/注销 ──

    def register(self, handle: AgentHandle) -> None:
        """注册一个 Agent 运行实例。"""
        self._instances[handle.handle_id] = handle
        self._task_index[handle.task_id] = handle.handle_id

    def unregister(self, handle_id: str) -> None:
        """注销一个 Agent 运行实例。"""
        handle: AgentHandle | None = self._instances.pop(handle_id, None)
        if handle is not None:
            self._task_index.pop(handle.task_id, None)

    # ── 查询 ──

    def get(self, handle_id: str) -> AgentHandle | None:
        """按 handle_id 查询。"""
        return self._instances.get(handle_id)

    def get_by_task(self, task_id: str) -> AgentHandle | None:
        """按 task_id 查询当前 active handle。"""
        handle_id: str | None = self._task_index.get(task_id)
        if handle_id is None:
            return None
        return self.get(handle_id)

    def get_all(self) -> list[AgentHandle]:
        """返回所有已注册实例。"""
        return list(self._instances.values())

    def get_active(self) -> list[AgentHandle]:
        """返回所有非终端状态的实例。"""
        return [h for h in self._instances.values() if not h.status.is_terminal()]

    def list_by_agent(self, agent_id: str) -> list[AgentHandle]:
        """按 agent_id 筛选实例。"""
        return [h for h in self._instances.values() if h.agent_id == agent_id]
