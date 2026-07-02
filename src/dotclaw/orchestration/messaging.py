"""AgentMessaging —— A2A 通信层。

纯通信基础设施：路由 + 追踪 + 取消。不创建 Agent、不派生 Runtime、不执行 Task。

对标 A2A: Service Discovery + tasks/send 追踪 + tasks/cancel。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.identity import AgentIdentity
    from .registry import AgentRegistry
    from .task import Task


class AgentMessaging:
    """Agent 间通信层。

    职责：
    - route(agent_id) → AgentIdentity：查 Registry（对标 A2A Service Discovery）
    - send(task, identity)：注册 Task 到活跃追踪表
    - cancel(task_id)：取消正在执行的 Task（对标 A2A tasks/cancel）

    不负责：创建 runtime、构造 Agent、执行 Task（由调用方 Agent.send() 处理）。
    """

    def __init__(self, registry: "AgentRegistry") -> None:
        self._registry: AgentRegistry = registry
        self._active_tasks: dict[str, "Task"] = {}

    # ── 路由 ──

    def route(self, agent_id: str) -> "AgentIdentity | None":
        """按 agent_id 查找 Identity。

        Args:
            agent_id: 目标 Agent ID

        Returns:
            AgentIdentity，不存在则返回 None
        """
        return self._registry.get(agent_id)

    # ── 追踪 ──

    def send(self, task: "Task", target: "AgentIdentity") -> None:
        """注册 Task 到活跃追踪表。

        调用方在创建子 Agent 并启动执行前调用此方法注册追踪。

        Args:
            task: 要注册的 Task
            target: 目标 Agent 的 Identity
        """
        self._active_tasks[task.task_id] = task

    # ── 列表 ──

    def list_active(self) -> list["Task"]:
        """返回所有活跃 Task 列表。"""
        return list(self._active_tasks.values())

    # ── 取消 ──

    def cancel(self, task_id: str) -> bool:
        """取消正在执行的 Task。

        对标 A2A tasks/cancel。

        Args:
            task_id: 要取消的 Task ID

        Returns:
            True 表示成功取消，False 表示未找到
        """
        task: Task | None = self._active_tasks.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True
