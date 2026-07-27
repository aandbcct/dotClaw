"""Runtime 纯状态机使用的控制动作定义。"""

from __future__ import annotations

from enum import StrEnum


class AgentAction(StrEnum):
    """领域状态机要求 Application 执行的下一项原子动作。"""

    INVOKE_LLM = "invoke_llm"
    EXECUTE_TOOLS = "execute_tools"
    FINALIZE = "finalize"
    # WAIT 为旧状态机的等待动作，仍被未迁移的 execution.py 使用；
    # 新状态机统一以 SUSPEND 表达外部等待（审批 / delegation），调用方清零后删除。
    WAIT = "wait"
    SUSPEND = "suspend"
    HANDOFF_TARGET = "handoff_target"
