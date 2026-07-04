"""Runtime 包 —— Agent 编排引擎。

Runtime 是 dotClaw 的基础设施层，提供：
- Agent 调度与循环控制（AgentState 状态机）
- 内部问题拆解（Task 概念）
- LLM 调用与工具执行协调（Runtime 编排引擎）
- Handoff 多 Agent 流转

架构关系：
    Runtime（编排引擎）
      ├── AgentState（状态机）→ 驱动单次 AgentRun 的 ReAct 循环
      └── Task（内部问题拆解）→ Agent 的计划-执行子任务

Session 管理对话持久化，AgentState 管理执行状态，Task 管理内部问题拆解。
"""

from .agent_state import AgentPhase, AgentState, AgentEvent, AgentAction, AgentStatus, AgentStartEvent, LLMResponseEvent, ToolsDoneEvent
from .task import Task, TaskProgress
from .runtime import Runtime

__all__ = [
    "Runtime",
    "AgentPhase",
    "AgentState",
    "AgentEvent",
    "AgentAction",
    "AgentStatus",
    "AgentStartEvent",
    "LLMResponseEvent",
    "ToolsDoneEvent",
    "Task",
    "TaskProgress",
]
