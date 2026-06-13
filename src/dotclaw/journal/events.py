"""Journal 事件定义。

16 种标准化事件覆盖 5 个域：
会话 / ReAct 循环 / LLM 调用 / 工具调用 / Skill+记忆+错误
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    """Journal 运行时事件。

    Attributes:
        timestamp: Unix 时间戳（秒）。
        created_at: 人类可读时间（HH:MM:SS.ms）。
        event_type: 事件类型标识，见 EventType。
        data: 事件携带的结构化数据。
    """

    timestamp: float
    created_at: str
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


class EventType:
    """16 种标准事件类型常量。"""

    # ── 会话 ──
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    # ── ReAct 循环 ──
    LOOP_START = "react.loop_start"
    LOOP_END = "react.loop_end"
    EMPTY_ACTION = "react.empty_action"

    # ── LLM 调用（四阶段） ──
    PROMPT_BUILT = "llm.prompt_built"
    LLM_CALL_START = "llm.call_start"
    LLM_CALL_END = "llm.call_end"
    LLM_RESPONSE_START = "llm.response_start"
    LLM_RESPONSE_END = "llm.response_end"

    # ── 工具调用 ──
    TOOL_START = "tool.call_start"
    TOOL_END = "tool.call_end"

    # ── Skill ──
    SKILL_BODY_LOADED = "skill.body_loaded"
    SKILL_REFERENCE = "skill.reference_load"
    SKILL_SCRIPT_EXEC = "skill.script_exec"

    # ── 记忆 ──
    MEMORY_RETRIEVAL = "memory.retrieval"
    MEMORY_WRITE = "memory.write"

    # ── 错误 ──
    ERROR = "system.error"
