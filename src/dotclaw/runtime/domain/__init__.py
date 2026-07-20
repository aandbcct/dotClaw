"""
Runtime v3 纯领域层公开接口，只导出事实、事件和状态规则。
定义“系统中什么是事实、哪些规则永远成立”
存放：实体、值对象、状态机、领域事件、不变量
"""

from .control import AgentAction
from .events import RunEvent, RunEventType
from .context import ContextVersion
from .facts import AgentRun, ContextCompactionScope, RunStatus
from .state import AgentPhase, AgentState

__all__ = [
    "AgentAction",
    "AgentPhase",
    "AgentRun",
    "AgentState",
    "ContextCompactionScope",
    "ContextVersion",
    "RunEvent",
    "RunEventType",
    "RunStatus",
]
