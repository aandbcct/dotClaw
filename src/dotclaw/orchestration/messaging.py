"""AgentMessaging —— A2A 通信寻址与任务追踪账本。

该类只负责 route 和 task tracking，不创建 Agent、不派生 Runtime、不执行 Task。
真实生命周期由 AgentDispatcher 管理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.identity import AgentIdentity
    from .registry import AgentRegistry
    from .task import Task


class AgentMessaging:
    """Agent 间通信基础设施。"""

    def __init__(self, registry: "AgentRegistry") -> None:
        self._registry: AgentRegistry = registry
        self._active_tasks: dict[str, Task] = {}
        self._task_targets: dict[str, str] = {}

    # ── 路由 ──

    def route(self, agent_id: str) -> "AgentIdentity | None":
        """按 agent_id 查找 Identity。"""
        return self._registry.get(agent_id)

    # ── 追踪 ──

    def track(self, task: "Task", target: "AgentIdentity") -> None:
        """注册 Task 到活跃追踪表。"""
        self._active_tasks[task.task_id] = task
        self._task_targets[task.task_id] = target.agent_id

    def send(self, task: "Task", target: "AgentIdentity") -> None:
        """兼容旧接口：注册 Task 到活跃追踪表。"""
        self.track(task, target)

    def untrack(self, task_id: str) -> None:
        """从活跃追踪表移除 Task。"""
        self._active_tasks.pop(task_id, None)
        self._task_targets.pop(task_id, None)

    def get_task(self, task_id: str) -> "Task | None":
        """按 task_id 查询 Task。"""
        return self._active_tasks.get(task_id)

    # ── 列表 ──

    def list_active_tasks(self) -> list["Task"]:
        """返回所有活跃 Task 列表。"""
        return list(self._active_tasks.values())

    def list_active(self) -> list["Task"]:
        """兼容旧接口：返回所有活跃 Task 列表。"""
        return self.list_active_tasks()

    # ── 兼容取消 ──

    def cancel(self, task_id: str) -> bool:
        """兼容旧接口：只标记 Task 取消，不负责真实运行实例取消。"""
        task: Task | None = self._active_tasks.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True
