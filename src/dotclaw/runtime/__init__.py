"""Runtime 包 —— Agent 编排引擎。

Runtime 是 dotClaw 的基础设施层，提供：
- 状态持久化（State + StateStore）
- 任务调度与循环控制（TaskState 状态机）
- 消息路由与错误恢复（Runtime 编排引擎）

架构关系：
    Runtime（编排引擎）
      ├── StateStore（状态持久化接口）
      │     └── SQLiteStateStore（默认实现）
      ├── State（会话级持久状态，与 thread_id 绑定）
      └── TaskState（任务级状态机，驱动单次 AgentRun 的 ReAct 循环）

Agent 是 Runtime 上调度的无状态"程序"（配置+Prompt+Tools），
多次 Task（AgentRun）通过同一 thread_id 共享读写同一份 State。
"""

from .state import State
from .state_store import StateStore, SQLiteStateStore
from .task_state import TaskPhase, TaskState, TaskEvent, TaskAction, TaskStatus
from .runtime import Runtime

__all__ = [
    "State",
    "StateStore",
    "SQLiteStateStore",
    "TaskPhase",
    "TaskState",
    "TaskEvent",
    "TaskAction",
    "TaskStatus",
    "Runtime",
]
