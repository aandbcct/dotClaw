"""AgentHandle —— Agent 实例的访问令牌。

不可变的 Agent 实例标识符。外部通过 Handle 与运行中的 Agent 实例交互，
内部状态由 Agent/AgentInstanceManager 维护。Handle 本身不包含内部逻辑或上下文。

对标 A2A: Task handle —— 通过 handle_id 追踪 Agent 实例生命周期。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from .task import Task, TaskStatus


# ============================================================================
# AgentStatus
# ============================================================================


class AgentStatus(Enum):
    """Agent 实例状态枚举。"""

    IDLE = "idle"
    """实例已创建，等待执行"""

    RUNNING = "running"
    """执行中"""

    COMPLETED = "completed"
    """正常完成"""

    FAILED = "failed"
    """执行失败"""

    KILLED = "killed"
    """被取消/终止"""

    def is_terminal(self) -> bool:
        """是否已进入终止状态。"""
        return self in (
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.KILLED,
        )


# ============================================================================
# AgentHandle
# ============================================================================


@dataclass
class AgentHandle:
    """Agent 实例的访问令牌。

    字段：
        handle_id: Handle 唯一标识
        agent_id: 关联的 Agent Identity ID
        task: 当前执行的 Task（输入+输出载体）
        status: Agent 实例状态

    外部通过 Handle 查询状态或等待结果，不可通过 Handle 修改内部状态。
    """

    handle_id: str
    """Handle 唯一标识"""

    agent_id: str
    """关联的 Agent Identity ID"""

    task: Task
    """当前执行的 Task"""

    status: AgentStatus
    """Agent 实例状态（内部方法更新，外部只读）"""

    def __init__(self, handle_id: str, agent_id: str, task: Task) -> None:
        self.handle_id = handle_id
        self.agent_id = agent_id
        self.task = task
        self.status = AgentStatus.IDLE

    # ── 生命周期（内部方法：由 AgentInstanceManager/Dispatcher 调用）──

    def _mark_running(self) -> None:
        self.status = AgentStatus.RUNNING

    def _mark_completed(self) -> None:
        self.status = AgentStatus.COMPLETED

    def _mark_failed(self) -> None:
        self.status = AgentStatus.FAILED

    def _mark_killed(self) -> None:
        self.status = AgentStatus.KILLED

    # ── 外部接口 ──

    async def result(self, timeout: float | None = None) -> Task:
        """等待 Agent 实例完成，返回 Task。

        Args:
            timeout: 超时秒数

        Returns:
            包含执行结果的 Task
        """
        return await self.task.result(timeout=timeout)

    def cancel(self) -> None:
        """取消执行。设置 killed 状态并通知等待者。"""
        self.task.cancel()
        self._mark_killed()
