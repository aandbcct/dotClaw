"""
Runtime v4 纯领域层公开接口，只导出事实、事件和状态规则。
定义“系统中什么是事实、哪些规则永远成立”
存放：实体、值对象、状态机、领域事件、不变量
"""

from .control import AgentAction
from .events import RunEvent, RunEventType
from .events import (
    AbandonRequested,
    AgentRunEvent,
    ApprovalGranted,
    ApprovalRejected,
    CancelRequested,
    DelegationCompleted,
    DelegationRequested,
    DelegationSubmitted,
    LLMCallFailed,
    LLMResponseProduced,
    RunStarted,
    TimeoutReached,
    ToolApprovalRequired,
    ToolBatchCompleted,
    ToolBatchFailed,
)
from .context import ContextVersion
from .facts import AgentRun, ContextCompactionScope, RunStatus
from .state import (
    AgentPhase,
    AgentRunState,
    AgentState,
    Created,
    Ended,
    InvalidTransition,
    RunMode,
    RunOutcome,
    RunStage,
    StateTransition,
    Suspended,
    SuspendReason,
    Running,
    transition,
)

__all__ = [
    "AbandonRequested",
    "AgentAction",
    "AgentPhase",
    "AgentRun",
    "AgentRunEvent",
    "AgentRunState",
    "AgentState",
    "ApprovalGranted",
    "ApprovalRejected",
    "CancelRequested",
    "ContextCompactionScope",
    "ContextVersion",
    "Created",
    "DelegationCompleted",
    "DelegationRequested",
    "DelegationSubmitted",
    "Ended",
    "InvalidTransition",
    "LLMCallFailed",
    "LLMResponseProduced",
    "RunEvent",
    "RunEventType",
    "RunMode",
    "RunOutcome",
    "RunStage",
    "RunStarted",
    "RunStatus",
    "StateTransition",
    "Suspended",
    "SuspendReason",
    "TimeoutReached",
    "ToolApprovalRequired",
    "ToolBatchCompleted",
    "ToolBatchFailed",
    "Running",
    "transition",
]
