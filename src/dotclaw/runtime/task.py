"""Task —— Agent 内部问题拆解单元。

Task 是 Agent 对复杂问题的分层拆解：进入计划模式时，Agent 通过工具
（如 todo_write）将目标分解为有序的子任务列表。每个 Task 拥有独立的
生命周期状态，由后续的 AgentRun 逐步推进完成。

与 orchestration/task.py 的区别：
   - 本模块 Task：Agent **内部** 的问题拆解（计划-执行），PENDING→IN_PROGRESS→COMPLETED
   - orchestration Task：Agent **之间** 的通信载体（A2A 协议），SUBMITTED→WORKING→COMPLETED

Task 的生命周期由 AgentState 管理，AgentState.tasks 维护当前会话的任务清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


# ============================================================================
# TaskProgress —— 任务进度枚举
# ============================================================================

class TaskProgress(Enum):
    """Task 内部拆解任务的进度状态。

    PENDING → IN_PROGRESS → COMPLETED / FAILED
    也支持 PENDING → CANCELED（直接取消）。
    """

    PENDING = "pending"
    """待处理，尚未开始执行"""

    IN_PROGRESS = "in_progress"
    """执行中"""

    COMPLETED = "completed"
    """已完成"""

    FAILED = "failed"
    """执行失败"""

    CANCELED = "canceled"
    """已取消"""


# ============================================================================
# Task —— 内部拆解任务
# ============================================================================

@dataclass
class Task:
    """Agent 内部的子任务。

    当一个复杂目标被拆解为多个步骤时，每个步骤创建一个 Task。
    Task 之间可以通过 parent_task_id 形成父子树。

    字段：
        task_id: 任务唯一标识
        description: 任务描述（如"读取配置文件"）
        progress: 当前执行进度
        parent_task_id: 父任务 ID（树形拆解，无则为 None）
        agent_id: 执行此任务的 Agent ID
        agent_run_ids: 关联的 AgentRun ID 列表（一个 Task 可能跨多次 AgentRun）
        result: 执行结果（COMPLETED 时填充）
        error: 错误信息（FAILED 时填充）
        created_at: 创建时间
        updated_at: 最后更新时间
    """

    task_id: str
    """任务唯一标识（通常为 8 位 hex）"""

    description: str
    """任务描述，说明此步骤要完成什么"""

    progress: TaskProgress = TaskProgress.PENDING
    """当前进度状态"""

    parent_task_id: str | None = None
    """父任务 ID。顶层任务为 None，子任务指向父 Task.task_id。"""

    agent_id: str = ""
    """执行此任务的 Agent ID"""

    agent_run_ids: list[str] = field(default_factory=list)
    """关联的 AgentRun ID 列表。一次 Task 可能跨越多次 AgentRun（如工具中断后恢复）。"""

    result: str | None = None
    """执行结果。仅在 progress=COMPLETED 时填充。"""

    error: str | None = None
    """错误信息。仅在 progress=FAILED 时填充。"""

    created_at: str = ""
    """创建时间（ISO 8601）"""

    updated_at: str = ""
    """最后更新时间（ISO 8601）"""

    # ======================== 原子操作：状态转换 ========================

    def mark_in_progress(self) -> None:
        """将 Task 从 PENDING 转为 IN_PROGRESS。

        原子操作：
        1. 校验当前 progress 必须是 PENDING
        2. 设置 progress=IN_PROGRESS
        3. 更新 updated_at

        Raises:
            ValueError: 当前状态不是 PENDING
        """
        if self.progress != TaskProgress.PENDING:
            raise ValueError(
                f"无法从 {self.progress} 转为 IN_PROGRESS，当前状态必须是 PENDING"
            )
        self.progress = TaskProgress.IN_PROGRESS
        self._touch()

    def mark_completed(self, result: str) -> None:
        """将 Task 从 IN_PROGRESS 转为 COMPLETED。

        原子操作：
        1. 校验当前 progress 必须是 IN_PROGRESS
        2. 设置 progress=COMPLETED 并写入 result
        3. 更新 updated_at

        Args:
            result: 执行结果文本

        Raises:
            ValueError: 当前状态不是 IN_PROGRESS
        """
        if self.progress != TaskProgress.IN_PROGRESS:
            raise ValueError(
                f"无法从 {self.progress} 转为 COMPLETED，当前状态必须是 IN_PROGRESS"
            )
        self.progress = TaskProgress.COMPLETED
        self.result = result
        self._touch()

    def mark_failed(self, error: str) -> None:
        """将 Task 从 IN_PROGRESS 转为 FAILED。

        原子操作：
        1. 校验当前 progress 必须是 IN_PROGRESS
        2. 设置 progress=FAILED 并写入 error
        3. 更新 updated_at

        Args:
            error: 错误描述

        Raises:
            ValueError: 当前状态不是 IN_PROGRESS
        """
        if self.progress != TaskProgress.IN_PROGRESS:
            raise ValueError(
                f"无法从 {self.progress} 转为 FAILED，当前状态必须是 IN_PROGRESS"
            )
        self.progress = TaskProgress.FAILED
        self.error = error
        self._touch()

    def mark_canceled(self) -> None:
        """将 Task 从 PENDING 转为 CANCELED。

        原子操作：
        1. 校验当前 progress 必须是 PENDING
        2. 设置 progress=CANCELED
        3. 更新 updated_at

        Raises:
            ValueError: 当前状态不是 PENDING
        """
        if self.progress != TaskProgress.PENDING:
            raise ValueError(
                f"无法从 {self.progress} 转为 CANCELED，当前状态必须是 PENDING"
            )
        self.progress = TaskProgress.CANCELED
        self._touch()

    # ======================== 原子操作：关联管理 ========================

    def add_agent_run(self, run_id: str) -> None:
        """关联一个 AgentRun ID。

        原子操作：
        1. 追加 run_id 到 agent_run_ids 列表
        2. 更新 updated_at

        Args:
            run_id: AgentRun 的 run_id
        """
        self.agent_run_ids.append(run_id)
        self._touch()

    # ======================== 辅助方法 ========================

    def _touch(self) -> None:
        """更新时间戳。"""
        self.updated_at = datetime.now(timezone.utc).isoformat()
