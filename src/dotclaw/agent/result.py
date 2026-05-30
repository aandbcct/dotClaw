"""AgentResult — 标准化 Agent 执行结果

纯 dataclass，零 dotClaw 内部依赖。
通过 __str__ 保持 str 兼容，渐进式迁移不破坏现有调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentResult:
    """一次 Agent.run() 调用的完整结果"""

    final_text: str = ""
    """LLM 最终回复文本"""

    tool_calls_count: int = 0
    """本次对话中工具调用的总次数"""

    iterations: int = 0
    """ReAct 循环的迭代次数"""

    duration_ms: int = 0
    """从 run() 进入到返回的耗时（毫秒）"""

    error: str | None = None
    """异常信息，正常完成为 None"""

    request_id: str = ""
    """本次请求的唯一标识"""

    def __str__(self) -> str:
        """保持 str 兼容：str(result) == result.final_text"""
        return self.final_text

    def __eq__(self, other: object) -> bool:
        """兼容 str 比较：AgentResult == "text" 等同于 AgentResult.final_text == "text" """
        if isinstance(other, str):
            return self.final_text == other
        return super().__eq__(other)

    def __contains__(self, item: str) -> bool:
        """兼容 str 成员检查：item in AgentResult 等同于 item in AgentResult.final_text"""
        return item in self.final_text
