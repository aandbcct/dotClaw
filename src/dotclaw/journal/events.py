"""Journal 事件定义。

18 种标准化事件覆盖 5 个域：
会话 / ReAct 循环 / LLM 调用 / 工具调用 / Skill+记忆+错误
+ Trace Message（消息内容作为 Trace Event 入 trace.jsonl）
+ State Change（状态变更事件）
"""

from dataclasses import dataclass, field
from enum import Enum
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
    """18 种标准事件类型常量。"""

    # ── 会话 ──
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    # ── ReAct 循环（由 TurnLoop 继承使用） ──
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
    TOOL_POLICY = "tool.policy_resolved"
    TOOL_APPROVAL = "tool.approval_outcome"

    # ── Skill ──
    SKILL_BODY_LOADED = "skill.body_loaded"
    SKILL_REFERENCE = "skill.reference_load"
    SKILL_SCRIPT_EXEC = "skill.script_exec"

    # ── 记忆 ──
    MEMORY_RETRIEVAL = "memory.retrieval"
    MEMORY_WRITE = "memory.write"

    # ── 错误 ──
    ERROR = "system.error"

    # ── Trace Message（消息内容作为 Trace Event 入 trace.jsonl）─
    TRACE_MESSAGE = "trace.message"
    """对话消息内容事件。包含 role/content/tool_calls/tool_call_id 等完整消息字段。
    所有流转消息（user/assistant/tool）均以此类型写入 trace.jsonl。"""

    # ── State Change（状态变更事件）─
    STATE_CHANGE = "state.change"
    """AgentState 变更事件。包含 phase/end_status 等状态转换信息。"""

    TASK_LIFECYCLE = "task.lifecycle"
    """Task 控制面生命周期事件，不包含任何跨 Session 的消息正文。"""


class TaskEventType(str, Enum):
    """同进程 delegation 可观测的生命周期动作。"""

    CREATED = "created"
    TARGET_STARTED = "target_started"
    MESSAGE_SENT = "message_sent"
    STATE_CHANGED = "state_changed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"
    PROTOCOL_VIOLATION = "protocol_violation"


class TraceMessageRole(Enum):
    """Trace Message 中的消息角色。"""
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"
