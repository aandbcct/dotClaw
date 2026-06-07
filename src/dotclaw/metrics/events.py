"""Agent 运行时事件定义。

14 种事件类型覆盖 ReAct 循环、工具调用、Skill、记忆、LLM、会话六个维度。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    """Agent 运行时事件。

    Attributes:
        timestamp: Unix 时间戳（毫秒）。
        event_type: 事件类型标识，见 EventType。
        data: 事件携带的自由格式数据。
    """

    timestamp: float
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


class EventType:
    """14 种标准事件类型常量。"""

    REACT_LOOP_START = "react.loop_start"
    REACT_LOOP_END = "react.loop_end"
    REACT_EMPTY_ACTION = "react.empty_action"

    TOOL_CALL_START = "tool.call_start"
    TOOL_CALL_END = "tool.call_end"

    SKILL_TRIGGER = "skill.trigger"
    SKILL_BODY_LOADED = "skill.body_loaded"
    SKILL_SCRIPT_EXEC = "skill.script_exec"

    MEMORY_RETRIEVAL = "memory.retrieval"
    MEMORY_WRITE = "memory.write"

    LLM_REQUEST_START = "llm.request_start"
    LLM_REQUEST_END = "llm.request_end"

    SESSION_START = "session.start"
    SESSION_END = "session.end"
