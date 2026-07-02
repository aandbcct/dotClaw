"""编排层 —— 多 Agent 协调、通信、调度。

agent/ 包负责单 Agent（身份、执行、上下文、生命周期），
orchestration/ 包负责多 Agent（注册、通信、编排、控制、调度）。
"""

from .task import Task, TaskStatus
from .registry import AgentRegistry
from .messaging import AgentMessaging
from .handle import AgentHandle, AgentStatus
from .agent_message import AgentMessage, AgentMessageType
from .instance_manager import AgentInstanceManager

__all__ = [
    "Task", "TaskStatus",
    "AgentRegistry",
    "AgentMessaging",
    "AgentHandle", "AgentStatus",
    "AgentMessage", "AgentMessageType",
    "AgentInstanceManager",
]
