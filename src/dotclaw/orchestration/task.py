"""Task —— Agent 间通信的聚合实体。

对标 A2A Task：是 Agent 间最小的通信单位。
父 Agent 创建并填充输入侧字段，目标 Agent 执行后填充结构化 TaskResult。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias

from ..agent.artifact import Artifact


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


# ============================================================================
# 枚举
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
        """是否已进入终止状态。"""
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED)


class TaskTargetKind(Enum):
    """任务目标类型。"""

    LOCAL = "local"
    """本机 Agent"""

    REMOTE = "remote"
    """远程 Agent"""


# ============================================================================
# TaskResult
# ============================================================================


@dataclass
class TaskResult:
    """子 Agent 完成后的结构化结果。

    summary 用于父 Agent 快速理解，content 保存完整结果，artifacts 保存文件或
    数据产物引用，metadata 保存 token、耗时、trace 等扩展信息。
    """

    summary: str = ""
    """结果摘要"""

    content: str = ""
    """完整结果正文"""

    artifacts: list[Artifact] = field(default_factory=list)
    """输出产物"""

    metadata: dict[str, JsonValue] = field(default_factory=dict)
    """扩展元数据"""

    def to_dict(self) -> dict[str, JsonValue]:
        """序列化为 dict。"""
        return {
            "summary": self.summary,
            "content": self.content,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, JsonValue]) -> "TaskResult":
        """从 dict 反序列化。"""
        raw_artifacts: JsonValue = data.get("artifacts", [])
        artifact_dicts: list[dict[str, JsonValue]] = []
        if isinstance(raw_artifacts, list):
            artifact_dicts = [item for item in raw_artifacts if isinstance(item, dict)]

        raw_metadata: JsonValue = data.get("metadata", {})
        metadata: dict[str, JsonValue] = (
            dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        )

        summary_value: JsonValue = data.get("summary", "")
        content_value: JsonValue = data.get("content", "")
        return cls(
            summary=summary_value if isinstance(summary_value, str) else "",
            content=content_value if isinstance(content_value, str) else "",
            artifacts=[Artifact.from_dict(a) for a in artifact_dicts],
            metadata=metadata,
        )


# ============================================================================
# Task
# ============================================================================


@dataclass
class Task:
    """Agent 间通信的聚合实体。

    Task 是业务委托单元；真实运行实例通过 AgentHandle 追踪。
    """

    task_id: str
    """任务唯一标识"""

    requester: str
    """发起 Agent 的 agent_id（兼容字段）"""

    description: str
    """任务描述。目标 Agent 将其作为 user_message 执行。"""

    target_agent_id: str = ""
    """目标 Agent ID"""

    target_kind: TaskTargetKind = TaskTargetKind.LOCAL
    """目标 Agent 类型"""

    context: str = ""
    """父 Agent 传入的必要上下文摘要。"""

    constraints: str = ""
    """约束条件。"""

    input_artifacts: list[Artifact] = field(default_factory=list)
    """父 Agent 传入的文件/数据引用。"""

    status: TaskStatus = TaskStatus.SUBMITTED
    """任务状态。"""

    final_result: str = ""
    """子 Agent 最终输出文本（兼容字段，优先使用 result）。"""

    task_result: TaskResult | None = None
    """结构化任务结果。"""

    output_artifacts: list[Artifact] = field(default_factory=list)
    """子 Agent 执行产出的产物列表（兼容字段）。"""

    error: str = ""
    """异常信息。"""

    parent_run_id: str = ""
    """父 Agent 的 AgentRun.run_id。"""

    sub_run_id: str = ""
    """子 Agent 的 AgentRun.run_id。"""

    active_handle_id: str = ""
    """当前承接该 Task 的运行实例句柄 ID。"""

    remote_task_id: str = ""
    """远程任务 ID，仅 target_kind=remote 时使用。"""

    created_at: str = ""
    """创建时间。"""

    updated_at: str = ""
    """最后更新时间。"""

    def __post_init__(self) -> None:
        """自动填充运行时等待事件。"""
        if not self.created_at:
            self.created_at = self._now()
        object.__setattr__(self, "_completion_event", asyncio.Event())
        if self.status.is_terminal():
            self._completion_event.set()

    @property
    def requester_agent_id(self) -> str:
        """发起 Agent ID。"""
        return self.requester

    def _notify_completion(self) -> None:
        """通知等待者 Task 已完成/失败/取消。"""
        event: asyncio.Event = getattr(self, "_completion_event")
        event.set()

    def _now(self) -> str:
        """返回 UTC ISO 时间。"""
        return datetime.now(timezone.utc).isoformat()

    def mark_working(self) -> None:
        """目标 Agent 开始执行。"""
        self.status = TaskStatus.WORKING
        self.updated_at = self._now()

    def mark_completed(
        self,
        final_result: str,
        output_artifacts: list[Artifact] | None = None,
        sub_run_id: str = "",
        task_result: TaskResult | None = None,
    ) -> None:
        """目标 Agent 执行完成。"""
        self.status = TaskStatus.COMPLETED
        self.final_result = final_result
        if output_artifacts is not None:
            self.output_artifacts = output_artifacts
        if task_result is not None:
            self.task_result = task_result
            self.final_result = task_result.content
            self.output_artifacts = list(task_result.artifacts)
        elif self.task_result is None:
            self.task_result = TaskResult(
                summary=final_result[:500],
                content=final_result,
                artifacts=list(self.output_artifacts),
            )
        self.sub_run_id = sub_run_id
        self.updated_at = self._now()
        self._notify_completion()

    def mark_failed(self, error: str) -> None:
        """目标 Agent 执行失败。"""
        self.status = TaskStatus.FAILED
        self.error = error
        self.updated_at = self._now()
        self._notify_completion()

    def mark_canceled(self) -> None:
        """任务被取消。"""
        self.status = TaskStatus.CANCELED
        self.updated_at = self._now()
        self._notify_completion()

    def is_terminal(self) -> bool:
        """Task 是否已进入终止状态。"""
        return self.status.is_terminal()

    async def result(self, timeout: float | None = None) -> "Task":
        """等待 Task 完成，返回自身。"""
        event: asyncio.Event = getattr(self, "_completion_event")
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return self

    def cancel(self) -> None:
        """取消 Task。"""
        self.mark_canceled()

    def to_dict(self) -> dict[str, JsonValue]:
        """序列化为 dict。"""
        return {
            "task_id": self.task_id,
            "requester": self.requester,
            "target_agent_id": self.target_agent_id,
            "target_kind": self.target_kind.value,
            "description": self.description,
            "context": self.context,
            "constraints": self.constraints,
            "input_artifacts": [a.to_dict() for a in self.input_artifacts],
            "status": self.status.value,
            "final_result": self.final_result,
            "task_result": self.task_result.to_dict() if self.task_result is not None else None,
            "output_artifacts": [a.to_dict() for a in self.output_artifacts],
            "error": self.error,
            "parent_run_id": self.parent_run_id,
            "sub_run_id": self.sub_run_id,
            "active_handle_id": self.active_handle_id,
            "remote_task_id": self.remote_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, JsonValue]) -> "Task":
        """从 dict 反序列化。"""
        status_raw: JsonValue = data.get("status", "submitted")
        target_kind_raw: JsonValue = data.get("target_kind", TaskTargetKind.LOCAL.value)
        result_raw: JsonValue = data.get("task_result")
        task_result: TaskResult | None = (
            TaskResult.from_dict(result_raw)
            if isinstance(result_raw, dict)
            else None
        )

        raw_input_artifacts: JsonValue = data.get("input_artifacts", [])
        input_artifact_dicts: list[dict[str, JsonValue]] = (
            [item for item in raw_input_artifacts if isinstance(item, dict)]
            if isinstance(raw_input_artifacts, list)
            else []
        )
        raw_output_artifacts: JsonValue = data.get("output_artifacts", [])
        output_artifact_dicts: list[dict[str, JsonValue]] = (
            [item for item in raw_output_artifacts if isinstance(item, dict)]
            if isinstance(raw_output_artifacts, list)
            else []
        )

        task_id_value: JsonValue = data.get("task_id", "")
        requester_value: JsonValue = data.get("requester", "")
        target_value: JsonValue = data.get("target_agent_id", "")
        description_value: JsonValue = data.get("description", "")
        context_value: JsonValue = data.get("context", "")
        constraints_value: JsonValue = data.get("constraints", "")
        final_value: JsonValue = data.get("final_result", "")
        error_value: JsonValue = data.get("error", "")
        parent_run_value: JsonValue = data.get("parent_run_id", "")
        sub_run_value: JsonValue = data.get("sub_run_id", "")
        handle_value: JsonValue = data.get("active_handle_id", "")
        remote_value: JsonValue = data.get("remote_task_id", "")
        created_value: JsonValue = data.get("created_at", "")
        updated_value: JsonValue = data.get("updated_at", "")

        return cls(
            task_id=task_id_value if isinstance(task_id_value, str) else "",
            requester=requester_value if isinstance(requester_value, str) else "",
            target_agent_id=target_value if isinstance(target_value, str) else "",
            target_kind=TaskTargetKind(target_kind_raw) if isinstance(target_kind_raw, str) else TaskTargetKind.LOCAL,
            description=description_value if isinstance(description_value, str) else "",
            context=context_value if isinstance(context_value, str) else "",
            constraints=constraints_value if isinstance(constraints_value, str) else "",
            input_artifacts=[Artifact.from_dict(a) for a in input_artifact_dicts],
            status=TaskStatus(status_raw) if isinstance(status_raw, str) else TaskStatus.SUBMITTED,
            final_result=final_value if isinstance(final_value, str) else "",
            task_result=task_result,
            output_artifacts=[Artifact.from_dict(a) for a in output_artifact_dicts],
            error=error_value if isinstance(error_value, str) else "",
            parent_run_id=parent_run_value if isinstance(parent_run_value, str) else "",
            sub_run_id=sub_run_value if isinstance(sub_run_value, str) else "",
            active_handle_id=handle_value if isinstance(handle_value, str) else "",
            remote_task_id=remote_value if isinstance(remote_value, str) else "",
            created_at=created_value if isinstance(created_value, str) else "",
            updated_at=updated_value if isinstance(updated_value, str) else "",
        )
