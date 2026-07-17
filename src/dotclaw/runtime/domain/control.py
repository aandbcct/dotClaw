"""Runtime 纯状态机使用的控制动作定义。"""

from __future__ import annotations

from enum import StrEnum


class AgentAction(StrEnum):
    """领域状态机要求 Application 执行的下一项原子动作。"""

    INVOKE_LLM = "invoke_llm"
    EXECUTE_TOOLS = "execute_tools"
    FINALIZE = "finalize"
    WAIT = "wait"
    HANDOFF_TARGET = "handoff_target"
