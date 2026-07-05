"""Runtime 包 —— Agent 编排引擎。

Runtime 是 dotClaw 的基础设施层，提供：
- Agent 调度与循环控制（TurnLoop 事件循环）
- Agent 状态机（AgentState）— 驱动单次 LLM 调用的状态转换
- 内部问题拆解（Task 概念）
- LLM 调用与工具执行协调（Runtime 原子方法）
- Handoff 多 Agent 流转
- 状态持久化（StateStore）

架构关系：
    TurnLoop（事件循环）
      ├── Runtime（原子执行能力）
      ├── AgentState（状态机）→ 驱动单次 LLM 调用的状态转换
      ├── StateStore（持久化）→ 跨 AgentRun 的状态快照
      └── Task（内部问题拆解）→ Agent 的计划-执行子任务

Session 管理对话持久化，AgentState 管理执行状态，Task 管理内部问题拆解。
"""

from .agent_state import AgentPhase, AgentState, AgentEvent, AgentAction, AgentStatus, AgentStartEvent, LLMResponseEvent, ToolsDoneEvent
from .task import Task, TaskProgress
from .runtime import Runtime
from .state_store import StateStore, StateSnapshot

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
    "StateStore",
    "StateSnapshot",
]
