"""Session 模块 —— 运行时上下文管理。

包含：
- Session — 运行时易失性上下文（加载 Conversation + 持有 LLM 上下文）
- AgentRun — 一次原子执行过程的结果
"""

from .session import Session
from .agent_run import AgentRun

__all__ = ["Session", "AgentRun"]
