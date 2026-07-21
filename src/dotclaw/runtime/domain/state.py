"""不依赖外部实现的 Runtime v4 Agent 状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .events import (
    ApprovalResolved,
    DelegationSubmitted,
    CancelRequested,
    DelegationCompleted,
    DomainEvent,
    LLMCompleted,
    LLMCompletionKind,
    RunStarted,
    TimeoutReached,
    ToolCompleted,
    ToolCompletionKind,
)
from .control import AgentAction


class AgentPhase(StrEnum):
    """状态机在一次运行中可处于的阶段。"""

    IDLE = "idle"
    WAITING_LLM = "waiting_llm"
    WAITING_TOOLS = "waiting_tools"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_DELEGATION = "waiting_delegation"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class AgentState:
    """只保存最小控制数据的纯领域状态。"""

    phase: AgentPhase = AgentPhase.IDLE
    iteration: int = 0
    retry_count: int = 0
    truncate_count: int = 0
    loop_fingerprint: str = ""
    waiting_control_id: str | None = None

    def transition(self, event: DomainEvent) -> StateTransition:
        """根据领域事件返回新的状态与下一项执行动作。"""
        if isinstance(event, CancelRequested):
            return StateTransition(self._cancel(), AgentAction.FINALIZE)
        if isinstance(event, TimeoutReached):
            return StateTransition(self._cancel(), AgentAction.FINALIZE)
        if isinstance(event, RunStarted):
            return self._on_run_started()
        if isinstance(event, LLMCompleted):
            return self._on_llm_completed(event)
        if isinstance(event, ToolCompleted):
            return self._on_tool_completed(event)
        if isinstance(event, ApprovalResolved):
            return self._on_approval_resolved(event)
        if isinstance(event, DelegationSubmitted):
            return self._on_delegation_submitted(event)
        if isinstance(event, DelegationCompleted):
            return self._on_delegation_completed(event)
        raise RuntimeError("不支持的领域事件")

    def is_terminal(self) -> bool:
        """判断状态机是否已经进入终态。"""
        terminal_phases: frozenset[AgentPhase] = frozenset({
            AgentPhase.COMPLETED,
            AgentPhase.FAILED,
            AgentPhase.CANCELLED,
            AgentPhase.INTERRUPTED,
            AgentPhase.ABANDONED,
        })
        return self.phase in terminal_phases

    def to_dict(self) -> dict[str, str | int | None]:
        """序列化为 Checkpoint 可保存的最小控制字段。"""
        return {
            "phase": self.phase.value,
            "iteration": self.iteration,
            "retry_count": self.retry_count,
            "truncate_count": self.truncate_count,
            "loop_fingerprint": self.loop_fingerprint,
            "waiting_control_id": self.waiting_control_id,
        }

    def _on_run_started(self) -> StateTransition:
        self._require_phase(AgentPhase.IDLE)
        next_state: AgentState = AgentState(phase=AgentPhase.WAITING_LLM, iteration=1)
        return StateTransition(next_state, AgentAction.INVOKE_LLM)

    def _on_llm_completed(self, event: LLMCompleted) -> StateTransition:
        self._require_phase(AgentPhase.WAITING_LLM)
        if event.kind is LLMCompletionKind.FINAL_RESPONSE:
            return StateTransition(self._with_phase(AgentPhase.FINALIZING), AgentAction.FINALIZE)
        if event.kind is LLMCompletionKind.TOOL_CALLS:
            return StateTransition(self._with_phase(AgentPhase.WAITING_TOOLS), AgentAction.EXECUTE_TOOLS)
        return StateTransition(self._with_phase(AgentPhase.FAILED), AgentAction.FINALIZE)

    def _on_tool_completed(self, event: ToolCompleted) -> StateTransition:
        self._require_phase(AgentPhase.WAITING_TOOLS)
        if event.kind is ToolCompletionKind.COMPLETED:
            next_state: AgentState = AgentState(
                phase=AgentPhase.WAITING_LLM,
                iteration=self.iteration + 1,
                retry_count=self.retry_count,
                truncate_count=self.truncate_count,
                loop_fingerprint=self.loop_fingerprint,
            )
            return StateTransition(next_state, AgentAction.INVOKE_LLM)
        if event.kind is ToolCompletionKind.APPROVAL_REQUIRED:
            waiting_state: AgentState = self._with_phase(
                AgentPhase.WAITING_APPROVAL,
                event.approval_id,
            )
            return StateTransition(waiting_state, AgentAction.WAIT)
        return StateTransition(self._with_phase(AgentPhase.FAILED), AgentAction.FINALIZE)

    def _on_approval_resolved(self, event: ApprovalResolved) -> StateTransition:
        self._require_phase(AgentPhase.WAITING_APPROVAL)
        if event.approval_id != self.waiting_control_id:
            raise RuntimeError("审批事件不属于当前等待控制项")
        if event.approved:
            next_state: AgentState = self._with_phase(AgentPhase.WAITING_TOOLS)
            return StateTransition(next_state, AgentAction.EXECUTE_TOOLS)
        return StateTransition(self._with_phase(AgentPhase.CANCELLED), AgentAction.FINALIZE)

    def _on_delegation_submitted(self, event: DelegationSubmitted) -> StateTransition:
        """进入等待子运行结果的状态，由 Engine 继续查询 DelegationPort。"""
        self._require_phase(AgentPhase.WAITING_TOOLS)
        return StateTransition(self._with_phase(AgentPhase.WAITING_DELEGATION), AgentAction.WAIT)

    def _on_delegation_completed(self, event: DelegationCompleted) -> StateTransition:
        self._require_phase(AgentPhase.WAITING_DELEGATION)
        if event.succeeded:
            return StateTransition(self._with_phase(AgentPhase.WAITING_LLM), AgentAction.INVOKE_LLM)
        return StateTransition(self._with_phase(AgentPhase.FAILED), AgentAction.FINALIZE)

    def _cancel(self) -> AgentState:
        return self._with_phase(AgentPhase.CANCELLED)

    def _with_phase(self, phase: AgentPhase, waiting_control_id: str | None = None) -> AgentState:
        return AgentState(
            phase=phase,
            iteration=self.iteration,
            retry_count=self.retry_count,
            truncate_count=self.truncate_count,
            loop_fingerprint=self.loop_fingerprint,
            waiting_control_id=waiting_control_id,
        )

    def _require_phase(self, expected_phase: AgentPhase) -> None:
        if self.phase is not expected_phase:
            raise RuntimeError(f"无效状态转换：当前为 {self.phase.value}，期望为 {expected_phase.value}")


@dataclass(frozen=True)
class StateTransition:
    """状态机处理一个领域事件后的结果。"""

    state: AgentState
    action: AgentAction
