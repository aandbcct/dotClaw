"""Runtime 包 —— Agent 编排引擎。

Runtime 是 dotClaw 的基础设施层，提供：
- 任务调度与循环控制（TaskState 状态机）
- LLM 调用与工具执行协调（Runtime 编排引擎）
- Handoff 多 Agent 流转

架构关系：
    Runtime（编排引擎）
      └── TaskState（任务级状态机，驱动单次 AgentRun 的 ReAct 循环）

Agent 是 Runtime 上调度的无状态"程序"（配置+Prompt+Tools）。
Session 管理对话持久化与恢复，TaskState 管理执行状态与 checkpoint/resume。
"""

from .task_state import TaskPhase, TaskState, TaskEvent, TaskAction, TaskStatus
from .runtime import Runtime

__all__ = [
    "Runtime",
    "TaskPhase",
    "TaskState",
    "TaskEvent",
    "TaskAction",
    "TaskStatus",
]
