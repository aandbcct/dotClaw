"""同进程 delegation 的领域模型。

本模块只定义稳定的数据契约：Task、消息、端点和状态机。消息投递、等待和
运行调度分别由 ``TaskMessageBroker`` 与 ``AgentDispatcher`` 负责，避免把
生命周期逻辑混入数据对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class TaskStatus(str, Enum):
    """Task 当前状态，表示下一步应由哪一端继续行动。"""

    SUBMITTED = "submitted"
    RUNNING_TARGET = "running_target"
    WAITING_SOURCE = "waiting_source"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def is_terminal(self) -> bool:
        """返回当前状态是否为终态。"""
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class TaskEndpoint(str, Enum):
    """Task 点对点通信的两个固定端点。"""

    SOURCE = "source"
    TARGET = "target"


class TaskMessageType(str, Enum):
    """MVP 支持的 Task 消息类型。"""

    REQUEST = "request"
    PROGRESS = "progress"
    QUESTION = "question"
    REPLY = "reply"
    CONTEXT_UPDATE = "context_update"
    RESULT = "result"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskSpecification:
    """不可变的委托任务契约，不能保存完整 Session 历史。"""

    title: str
    objective: str
    materials: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    expected_deliverables: list[str] = field(default_factory=list)

    def render_user_message(self) -> str:
        """将任务契约渲染为 target Session 的首条 user 消息。"""
        sections: list[str] = [f"任务：{self.title}", f"目标：{self.objective}"]
        if self.materials:
            sections.append("材料：\n" + "\n".join(f"- {item}" for item in self.materials))
        if self.constraints:
            sections.append("约束：\n" + "\n".join(f"- {item}" for item in self.constraints))
        if self.expected_deliverables:
            sections.append("预期交付物：\n" + "\n".join(f"- {item}" for item in self.expected_deliverables))
        return "\n\n".join(sections)


@dataclass(frozen=True)
class TaskMessage:
    """一条不可变的、按 sequence 排序的 Task 通信事实。"""

    task_id: str
    sequence: int
    sender: TaskEndpoint
    recipient: TaskEndpoint
    sender_session_id: str
    sender_run_id: str
    message_type: TaskMessageType
    payload: str
    created_at: str


@dataclass(frozen=True)
class TaskEndpointBinding:
    """一个端点可操作 Task 的 Identity 与 Session 绑定。"""

    endpoint: TaskEndpoint
    identity_id: str
    session_id: str


@dataclass
class Task:
    """内存生命周期内的 delegation 聚合。

    状态和终态结果只由 Broker 修改，调用方必须使用 Broker 的原子操作，防止
    消息流与状态机出现不一致。
    """

    task_id: str
    specification: TaskSpecification
    source: TaskEndpointBinding
    target: TaskEndpointBinding
    status: TaskStatus = TaskStatus.SUBMITTED
    result_message: TaskMessage | None = None
    error: str = ""
    cancellation_requested: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def binding_for(self, endpoint: TaskEndpoint) -> TaskEndpointBinding:
        """返回指定端点的身份与 Session 绑定。"""
        return self.source if endpoint is TaskEndpoint.SOURCE else self.target

    def touch(self) -> None:
        """更新状态投影时间戳，仅供 Broker 内部调用。"""
        self.updated_at = datetime.now(timezone.utc).isoformat()
