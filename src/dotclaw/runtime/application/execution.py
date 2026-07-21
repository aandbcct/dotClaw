"""一次 AgentRun 的内存执行事务对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..domain.control import AgentAction
from ..domain.context import ContextVersion, StagedHistoryCompression
from ..domain.facts import AgentPolicySnapshot, JSONMap, RunMessage
from dotclaw.runtime.domain.state import AgentState
from .context_budget import ContextBudgetDecision
from .dto import RunRequest


class PendingControlKind(StrEnum):
    """运行等待的外部控制类型。"""

    APPROVAL = "approval"
    DELEGATION = "delegation"


@dataclass(frozen=True)
class RunBudget:
    """运行期间累计的资源预算。"""

    max_iterations: int
    tokens_in: int = 0
    tokens_out: int = 0
    timeout_ms: int = 0

    def to_dict(self) -> JSONMap:
        """转换为 Checkpoint 可保存的预算数据。"""
        return {
            "max_iterations": self.max_iterations,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "timeout_ms": self.timeout_ms,
        }


@dataclass
class CancellationToken:
    """仅归属当前 RunExecution 的取消标记。"""

    cancelled: bool = False
    reason: str = ""

    def request(self, reason: str) -> None:
        """记录取消请求，供 Runtime 在安全点读取。"""
        self.cancelled = True
        self.reason = reason


@dataclass(frozen=True)
class PendingControl:
    """等待外部控制结果所需的最小引用。"""

    kind: PendingControlKind
    control_id: str

    def to_dict(self) -> JSONMap:
        """转换为 Checkpoint 可保存的数据。"""
        return {"kind": self.kind.value, "control_id": self.control_id}


@dataclass
class RunExecution:
    """运行期可变状态容器，结束后由 RuntimeEngine 销毁。"""

    run_id: str
    request: RunRequest
    policy: AgentPolicySnapshot
    state: AgentState
    budget: RunBudget
    message_cursor: int = 0
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    pending_control: PendingControl | None = None
    run_messages: tuple[RunMessage, ...] = ()
    """当前 Run 已持久化的执行消息，仅供 Port 构造后续上下文。"""
    has_streamed_text: bool = False
    """本次运行是否已向入口发送过模型文本增量。"""
    active_context_version: ContextVersion | None = None
    """最近一次已落盘的上下文版本；后续轮次和审批恢复必须重放该事实。"""
    staged_history_compressions: tuple[StagedHistoryCompression, ...] = ()
    """本 Run 尚未提交到 Session 的历史压缩候选引用。"""
    replay_active_context: bool = False
    """仅审批或中断重试时重放已冻结 Context Version，普通下一轮仍重新加载 Slot。"""
    context_budget_decision: ContextBudgetDecision | None = None
    """最近一次业务 LLM 调用前的真实输入预算结论，仅保存不含正文的审计字段。"""

    def view(self) -> RunExecutionView:
        """生成提供给 Port 的只读执行视图。"""
        return RunExecutionView(
            run_id=self.run_id,
            policy=self.policy,
            state=self.state,
            budget=self.budget,
            message_cursor=self.message_cursor,
            pending_control=self.pending_control,
            run_messages=self.run_messages,
            active_context_version=self.active_context_version,
            staged_history_compressions=self.staged_history_compressions,
            replay_active_context=self.replay_active_context,
        )

    def update_state(self, state: AgentState, action: AgentAction) -> None:
        """在 Runtime 处理完状态机转移后更新内存控制状态。"""
        self.state = state
        if action is not AgentAction.WAIT:
            self.pending_control = None

    def replace_run_messages(self, messages: tuple[RunMessage, ...]) -> None:
        """同步已持久化的运行消息，使后续 Port 可重放本 Run 的 ReAct 证据。"""
        self.run_messages = messages
        self.message_cursor = len(messages)

    def mark_text_streamed(self) -> None:
        """记录入口已收到模型文本，避免终态重复呈现相同内容。"""
        self.has_streamed_text = True

    def activate_context_version(self, context_version: ContextVersion) -> None:
        """绑定已落盘的最新上下文版本，禁止回退或覆盖同版本事实。"""
        current: ContextVersion | None = self.active_context_version
        if current is not None and context_version.version < current.version:
            raise ValueError("Context Version 禁止回退")
        if current is not None and context_version.version == current.version and context_version != current:
            raise ValueError("同一 Context Version 禁止覆盖为不同内容")
        self.active_context_version = context_version

    def record_context_budget_decision(self, decision: ContextBudgetDecision) -> None:
        """记录当前安全点的预算结论，供紧邻的 checkpoint 持久化。"""
        self.context_budget_decision = decision

    def to_dict(self) -> JSONMap:
        """序列化为不含外部实例引用的检查点数据。"""
        return {
            "run_id": self.run_id,
            "request": self.request.to_dict(),
            "policy": self.policy.to_dict(),
            "state": self.state.to_dict(),
            "budget": self.budget.to_dict(),
            "message_cursor": self.message_cursor,
            "cancelled": self.cancellation.cancelled,
            "cancellation_reason": self.cancellation.reason,
            "pending_control": None if self.pending_control is None else self.pending_control.to_dict(),
        }


@dataclass(frozen=True)
class RunExecutionView:
    """暴露给 Port 的执行期只读信息。"""

    run_id: str
    policy: AgentPolicySnapshot
    """本次运行冻结的 Agent 身份与上下文策略。"""
    state: AgentState
    budget: RunBudget
    message_cursor: int
    pending_control: PendingControl | None
    run_messages: tuple[RunMessage, ...] = ()
    """本 Run 已持久化的消息证据；不包含 Session 可变对象。"""
    active_context_version: ContextVersion | None = None
    """最近一次调用前保存的完整上下文版本；存在时 ContextPort 必须直接重放已冻结 Slot。"""
    staged_history_compressions: tuple[StagedHistoryCompression, ...] = ()
    """尚未提交到 Session 的候选控制信息。"""
    replay_active_context: bool = False
    """是否要求 ContextPort 直接重放活动版本。"""
