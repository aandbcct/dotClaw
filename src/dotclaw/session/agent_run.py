"""AgentRun —— 一次原子执行过程的结果。

AgentRun = 一次连续的 LLM 上下文窗口占用周期。
AgentLoop.run() 返回 AgentRun，包装 AgentResult + 执行后的 Session。

与 AgentLoop 的关系：
  一次 AgentLoop.run() 产生一个 AgentRun (M1 简单映射)
  M2: AgentRun 支持 suspend/resume，跨越多次 AgentLoop 片段

与 Session 的关系：
  1 AgentRun ∈ 1 Session
  AgentRun 完成后 Session.conversation.messages 已更新
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agent.result import AgentResult
from .session import Session


@dataclass
class AgentRun:
    """一次 Agent 执行的完整结果。

    包装 AgentResult + 执行后的 Session，同时保持 str 兼容性（与 AgentResult 相同）。

    属性代理确保 str(agent_run) == agent_run.result.final_text，
    保持与旧代码的向后兼容。
    """

    result: AgentResult = field(default_factory=AgentResult)
    """AgentResult（final_text / tool_calls_count / iterations / duration_ms / error）"""

    session: Session | None = None
    """执行后的 Session。conversation.messages 已被 AgentLoop finalize 更新。"""

    # ── 属性代理（向后兼容：str(AgentRun) == AgentResult.final_text）──

    @property
    def final_text(self) -> str:
        """LLM 最终回复文本（从 result 代理）。"""
        return self.result.final_text

    @property
    def tool_calls_count(self) -> int:
        """工具调用总次数（从 result 代理）。"""
        return self.result.tool_calls_count

    @property
    def iterations(self) -> int:
        """ReAct 循环迭代次数（从 result 代理）。"""
        return self.result.iterations

    @property
    def duration_ms(self) -> int:
        """执行耗时（毫秒，从 result 代理）。"""
        return self.result.duration_ms

    @property
    def error(self) -> str | None:
        """异常信息（从 result 代理）。"""
        return self.result.error

    @property
    def request_id(self) -> str:
        """请求标识（从 result 代理）。"""
        return self.result.request_id

    # ── str 兼容性 ──

    def __str__(self) -> str:
        """str(agent_run) == agent_run.result.final_text"""
        return str(self.result)

    def __eq__(self, other: object) -> bool:
        """保持 str 比较兼容：AgentRun == "text" 等同于 result.final_text == "text" """
        if isinstance(other, str):
            return self.result.final_text == other
        return super().__eq__(other)

    def __contains__(self, item: str) -> bool:
        """item in AgentRun 等同于 item in result.final_text"""
        return item in self.result.final_text
