"""AgentRunner 抽象 —— 本地和远程 Agent 执行能力的统一接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

from ..handle import AgentHandle
from ..task import Task, TaskResult

if TYPE_CHECKING:
    from ...agent.agent import Agent
    from ...runtime.runtime import Runtime


@dataclass
class SpawnContext:
    """提交 delegation 任务所需的父侧上下文。"""

    runtime: "Runtime"
    """父 Agent 当前 Runtime"""

    requester: "Agent"
    """发起委托的父 Agent"""

    parent_run_id: str = ""
    """父 AgentRun ID"""


class AgentRunner(Protocol):
    """Agent 执行器协议。"""

    async def submit(self, task: Task, context: SpawnContext) -> AgentHandle:
        """提交任务并返回运行实例句柄。"""
        ...

    async def wait(self, handle: AgentHandle, timeout: float | None = None) -> TaskResult:
        """等待运行实例完成并返回结构化结果。"""
        ...

    async def cancel(self, handle: AgentHandle) -> bool:
        """取消运行实例。"""
        ...
