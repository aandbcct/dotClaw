"""Task —— Agent 间通信的聚合实体。

对标 A2A Task：是 Agent 间最小的通信单位。
父 Agent 创建并填充输入侧字段 → 子 Agent 执行后填充输出侧字段。

主从式 spawn 的输入和输出载体：一个 Task 携带任务描述、上下文、产物、状态、错误信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .artifact import Artifact


# ============================================================================
# TaskStatus
# ============================================================================


class TaskStatus(Enum):
    """任务状态枚举。

    对标 A2A TaskState：submitted → working → completed / failed / canceled。
    """

    SUBMITTED = "submitted"
    """已提交，等待执行"""

    WORKING = "working"
    """执行中"""

    COMPLETED = "completed"
    """正常完成"""

    FAILED = "failed"
    """执行失败"""

    CANCELED = "canceled"
    """已被取消"""

    def is_terminal(self) -> bool:
        """是否已进入终止状态（completed / failed / canceled）。"""
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED)


# ============================================================================
# Task
# ============================================================================


@dataclass
class Task:
    """Agent 间通信的聚合实体。

    字段分为三组：
    - 标识：task_id / requester
    - 输入侧（父 Agent 填充）：description / context / constraints / input_artifacts / parent_run_id
    - 输出侧（子 Agent 填充）：status / final_result / output_artifacts / error / sub_run_id
    """

    # ── 标识 ──

    task_id: str
    """任务唯一标识"""

    requester: str
    """发起 Agent 的 agent_id。子 Agent 通过 AgentRegistry.get(requester) 查找 Identity。"""

    # ── 输入侧（父 Agent 填充）──

    description: str
    """任务描述。子 Agent 将其作为 user_message 执行。"""

    context: str = ""
    """父 Agent 传入的必要上下文摘要。"""

    constraints: str = ""
    """约束条件（如 "只用内置工具"）。"""

    input_artifacts: list[Artifact] = field(default_factory=list)
    """父 Agent 传入的文件/数据引用。"""

    # ── 输出侧（子 Agent 填充）──

    status: TaskStatus = TaskStatus.SUBMITTED
    """任务状态。"""

    final_result: str = ""
    """子 Agent 最终输出文本。"""

    output_artifacts: list[Artifact] = field(default_factory=list)
    """子 Agent 执行产出的产物列表。"""

    error: str = ""
    """异常信息（仅在 status=failed 时非空）。"""

    # ── 追踪 ──

    parent_run_id: str = ""
    """父 Agent 的 AgentRun.run_id。"""

    sub_run_id: str = ""
    """子 Agent 的 AgentRun.run_id（执行完毕后由子填充）。"""

    # ── 时间戳 ──

    created_at: str = ""
    """创建时间（ISO 8601）。"""

    updated_at: str = ""
    """最后更新时间（ISO 8601）。"""

    # ── 生命周期方法 ──

    def __post_init__(self) -> None:
        """自动填充 created_at（如果为空）。"""
        if not self.created_at:
            self.created_at = self._now()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def mark_working(self) -> None:
        """子 Agent 开始执行。status = working。"""
        self.status = TaskStatus.WORKING
        self.updated_at = self._now()

    def mark_completed(
        self,
        final_result: str,
        output_artifacts: list[Artifact] | None = None,
        sub_run_id: str = "",
    ) -> None:
        """子 Agent 执行完成。填充 status + 结果。"""
        self.status = TaskStatus.COMPLETED
        self.final_result = final_result
        if output_artifacts is not None:
            self.output_artifacts = output_artifacts
        self.sub_run_id = sub_run_id
        self.updated_at = self._now()

    def mark_failed(self, error: str) -> None:
        """子 Agent 执行失败。填充 status + error。"""
        self.status = TaskStatus.FAILED
        self.error = error
        self.updated_at = self._now()

    def mark_canceled(self) -> None:
        """父 Agent 取消任务。status = canceled。"""
        self.status = TaskStatus.CANCELED
        self.updated_at = self._now()

    def is_terminal(self) -> bool:
        """Task 是否已进入终止状态。委托给 TaskStatus。"""
        return self.status.is_terminal()

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为 dict。Artifact 字段转为 dict 列表。"""
        return {
            "task_id": self.task_id,
            "requester": self.requester,
            "description": self.description,
            "context": self.context,
            "constraints": self.constraints,
            "input_artifacts": [a.to_dict() for a in self.input_artifacts],
            "status": self.status.value,
            "final_result": self.final_result,
            "output_artifacts": [a.to_dict() for a in self.output_artifacts],
            "error": self.error,
            "parent_run_id": self.parent_run_id,
            "sub_run_id": self.sub_run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        """从 dict 反序列化。"""
        status_raw: str = data.get("status", "submitted")
        return cls(
            task_id=data.get("task_id", ""),
            requester=data.get("requester", ""),
            description=data.get("description", ""),
            context=data.get("context", ""),
            constraints=data.get("constraints", ""),
            input_artifacts=[Artifact.from_dict(a) for a in data.get("input_artifacts", [])],
            status=TaskStatus(status_raw),
            final_result=data.get("final_result", ""),
            output_artifacts=[Artifact.from_dict(a) for a in data.get("output_artifacts", [])],
            error=data.get("error", ""),
            parent_run_id=data.get("parent_run_id", ""),
            sub_run_id=data.get("sub_run_id", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
