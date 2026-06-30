"""Session —— 运行时易失性上下文。

Session 是 Runtime 概念：Agent 执行过程中依赖的易失性上下文。
加载一个持久化的 Conversation，持有当前执行周期的 LLM 上下文 (history)。

与 Conversation 的关系：
  1 Session ↔ 1 Conversation（Session 加载一个 Conversation）
  1 Conversation 可被多个 Session 加载（如续接历史对话）

与 AgentRun 的关系：
  1 AgentRun ∈ 1 Session（一次 AgentLoop.run() 产生一个 AgentRun）
  Multi-Agent: 父子 Session 独立（fork 模式）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.base import Message
from ..storage.conversation import Conversation


@dataclass
class Session:
    """运行时易失性上下文。

    属性：
        conversation: 加载的持久化对话记录
        history: 易失性 LLM 上下文（包含本轮 assistant tool_calls + tool_result）
    """

    conversation: Conversation
    """加载的持久化对话记录。AgentLoop finalize 时更新其 messages。"""

    history: list[Message] = field(default_factory=list)
    """易失性 LLM 上下文。

    存储当前 AgentRun 的 ReAct 循环中产生的 assistant(tool_calls) 和 tool result 消息。
    每轮 _build_messages 将 conversation.messages + history + 当前 user_message 组装为 LLM 输入。
    AgentLoop finalize 后清空（下一轮 AgentRun 新建 Session 时重新开始）。

    不同于 conversation.messages（持久化的 user/assistant 文本）：
    history 包含 ReAct 内部的 tool_call + tool_result 消息，不持久化。
    """
