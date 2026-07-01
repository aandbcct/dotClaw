"""AgentState —— 一次请求级别的运行状态累加器。

AgentState 跨 AgentRun 累加：一次用户请求可能产生多个 AgentRun
（父 spawn 子），AgentState 汇总所有 AgentRun 的指标。

生命周期：
  1. 请求开始时由 orchestrator（main.py / 父 Agent）创建
  2. 每跑完一个 AgentRun，调用 accumulate(run) 累加
  3. 请求结束时产出最终状态
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent_run import AgentRun


@dataclass
class AgentState:
    """一次请求级别的运行状态。

    字段：
        request_id: 请求唯一标识
        tool_calls_total: 跨所有 AgentRun 的工具调用总次数
        tokens_in_total: 跨所有 AgentRun 的输入 token 总数
        tokens_out_total: 跨所有 AgentRun 的输出 token 总数
        iterations_total: 跨所有 AgentRun 的 ReAct 迭代总次数
        duration_ms_total: 跨所有 AgentRun 的总耗时
        agent_run_ids: 本次请求产生的所有 AgentRun ID
        status: 最终状态（running / completed / failed）
        error: 最终异常信息
        started_at: 请求开始时间
        ended_at: 请求结束时间
    """

    request_id: str
    """请求唯一标识"""

    tool_calls_total: int = 0
    """工具调用总次数"""

    tokens_in_total: int = 0
    """输入 token 总数"""

    tokens_out_total: int = 0
    """输出 token 总数"""

    iterations_total: int = 0
    """ReAct 迭代总次数"""

    duration_ms_total: int = 0
    """总耗时（毫秒）"""

    agent_run_ids: list[str] = field(default_factory=list)
    """本次请求产生的所有 AgentRun ID"""

    status: str = "running"
    """最终状态：running / completed / failed"""

    error: str | None = None
    """异常信息（仅在 failed 时非空）"""

    started_at: str = ""
    """开始时间（ISO 8601）"""

    ended_at: str = ""
    """结束时间（ISO 8601）"""

    # ── 累加方法 ──

    def accumulate(self, run: AgentRun) -> None:
        """累加一个 AgentRun 的指标。

        Args:
            run: 刚完成的 AgentRun
        """
        self.tool_calls_total += run.tool_calls
        self.tokens_in_total += run.tokens_in
        self.tokens_out_total += run.tokens_out
        self.iterations_total += run.iterations
        self.duration_ms_total += run.duration_ms
        self.agent_run_ids.append(run.run_id)

    def finish(self, status: str, error: str | None = None) -> None:
        """标记请求结束。

        Args:
            status: 最终状态（completed / failed）
            error: 异常信息（failed 时提供）
        """
        from datetime import datetime
        self.status = status
        self.error = error
        self.ended_at = datetime.now().isoformat()
