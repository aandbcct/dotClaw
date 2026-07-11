"""AgentHandle —— Agent 运行实例的访问令牌。

Handle 表示一次 Task 的运行实例。Task 记录业务委托，Handle 记录运行时承接方，
包括本地 asyncio.Task 或远程 remote_task_id 等实例级信息。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .task import JsonValue, Task, TaskTargetKind


# ============================================================================
# 枚举
# ============================================================================


class AgentInstanceStatus(Enum):
    """Agent 运行实例状态枚举。"""

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
            AgentInstanceStatus.COMPLETED,
            AgentInstanceStatus.FAILED,
            AgentInstanceStatus.KILLED,
        )


class RunnerKind(Enum):
    """运行器类型。"""

    LOCAL = "local"
    """本地运行器"""

    REMOTE = "remote"
    """远程运行器"""

    @classmethod
    def from_target_kind(cls, target_kind: TaskTargetKind) -> "RunnerKind":
        """从任务目标类型映射运行器类型。"""
        if target_kind == TaskTargetKind.REMOTE:
            return cls.REMOTE
        return cls.LOCAL


# 兼容旧导入。新代码应使用 AgentInstanceStatus。
AgentStatus = AgentInstanceStatus


# ============================================================================
# AgentHandle
# ============================================================================


@dataclass
class AgentHandle:
    """Agent 运行实例的访问令牌。

    外部优先通过 task_id 管理委托任务；handle_id 用于实例级 trace、调试和取消映射。
    """

    handle_id: str
    """Handle 唯一标识"""

    agent_id: str
    """关联的目标 Agent ID"""

    task: Task
    """当前执行的 Task"""

    runner_kind: RunnerKind = RunnerKind.LOCAL
    """承接该实例的 runner 类型"""

    status: AgentInstanceStatus = AgentInstanceStatus.IDLE
    """Agent 实例状态"""

    asyncio_task: asyncio.Task[Task] | None = None
    """本地执行的 asyncio.Task，仅 local runner 使用"""

    remote_task_id: str = ""
    """远程任务 ID，仅 remote runner 使用"""

    metadata: dict[str, JsonValue] = field(default_factory=dict)
    """实例元数据"""

    created_at: str = ""
    """创建时间"""

    updated_at: str = ""
    """更新时间"""

    def __post_init__(self) -> None:
        """补齐时间戳并回写 Task 当前 handle。"""
        if not self.created_at:
            self.created_at = self._now()
        self.updated_at = self.created_at
        self.task.active_handle_id = self.handle_id
        if self.remote_task_id:
            self.task.remote_task_id = self.remote_task_id

    def _now(self) -> str:
        """返回 UTC ISO 时间。"""
        return datetime.now(timezone.utc).isoformat()

    def attach_asyncio_task(self, task: asyncio.Task[Task]) -> None:
        """绑定本地 asyncio.Task。"""
        self.asyncio_task = task
        self.updated_at = self._now()

    def attach_remote_task(self, remote_task_id: str) -> None:
        """绑定远程任务 ID。"""
        self.remote_task_id = remote_task_id
        self.task.remote_task_id = remote_task_id
        self.updated_at = self._now()

    # ── 生命周期（内部方法：由 Dispatcher/Runner 调用）──

    def _mark_running(self) -> None:
        self.status = AgentInstanceStatus.RUNNING
        self.updated_at = self._now()

    def _mark_completed(self) -> None:
        self.status = AgentInstanceStatus.COMPLETED
        self.updated_at = self._now()

    def _mark_failed(self) -> None:
        self.status = AgentInstanceStatus.FAILED
        self.updated_at = self._now()

    def _mark_killed(self) -> None:
        self.status = AgentInstanceStatus.KILLED
        self.updated_at = self._now()

    # ── 外部接口 ──

    @property
    def task_id(self) -> str:
        """关联 Task ID。"""
        return self.task.task_id

    async def result(self, timeout: float | None = None) -> Task:
        """等待 Agent 实例完成，返回 Task。"""
        return await self.task.result(timeout=timeout)

    def cancel(self) -> None:
        """取消执行，并同步标记 Task 与实例状态。"""
        if self.asyncio_task is not None and not self.asyncio_task.done():
            self.asyncio_task.cancel()
        self.task.cancel()
        self._mark_killed()
