"""Runtime 包 —— Agent 执行引擎。

Runtime 是 dotClaw 的执行引擎 + 依赖容器，提供：
- 执行入口（run）— 一次完整的用户消息 → Agent 回复
- Agent 状态机（AgentState）— 驱动 ReAct 循环的状态转换
- 内部问题拆解（Task）
- 工具执行与 LLM 调用协调
- Handoff 多 Agent 流转
- 状态持久化（StateStore）

架构关系：
    Runtime（执行引擎 + 依赖容器）
      ├── AgentState（状态机）→ 驱动 ReAct 循环的状态转换
      ├── StateStore（持久化）→ 跨 AgentRun 的状态快照
      └── Task（内部问题拆解）→ Agent 的计划-执行子任务
"""

from .agent_state import AgentPhase, AgentState, AgentEvent, AgentAction, AgentStatus, AgentStartEvent, LLMResponseEvent, ToolsDoneEvent
from .task import Task, TaskProgress
from .runtime import Runtime
from .state_store import StateStore, StateSnapshot
from .domain.models import AgentRun as RuntimeAgentRun, RunRequest, RunResult, RunStatus
from .domain.state import AgentState as RuntimeV2AgentState

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
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RuntimeAgentRun",
    "RuntimeV2AgentState",
]
