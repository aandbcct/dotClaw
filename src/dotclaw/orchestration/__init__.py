"""编排层 —— 多 Agent 协调、通信、调度。"""

from .task import Task, TaskStatus, TaskTargetKind, TaskResult
from .registry import AgentRegistry
from .messaging import AgentMessaging
from .handle import AgentHandle, AgentStatus, AgentInstanceStatus, RunnerKind
from .agent_message import AgentMessage, AgentMessageType
from .instance_manager import AgentInstanceManager
from .events import DelegationEvent, DelegationEventType
from .dispatcher import AgentDispatcher

__all__ = [
    "Task", "TaskStatus", "TaskTargetKind", "TaskResult",
    "AgentRegistry",
    "AgentMessaging",
    "AgentHandle", "AgentStatus", "AgentInstanceStatus", "RunnerKind",
    "AgentMessage", "AgentMessageType",
    "AgentInstanceManager",
    "DelegationEvent", "DelegationEventType",
    "AgentDispatcher",
]
