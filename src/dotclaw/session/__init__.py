"""Session 模块 —— 对话管理与执行记录。

包含：
- Session / Conversation / SessionManager — 持久化对话记录 + 运行时上下文
- AgentRun / AgentRunManager — 一次原子调用的完整记录
"""

from .session import Session, Conversation, SessionManager
from .agent_run import AgentRun, AgentRunManager

__all__ = [
    "Session", "Conversation", "SessionManager",
    "AgentRun", "AgentRunManager",
]
