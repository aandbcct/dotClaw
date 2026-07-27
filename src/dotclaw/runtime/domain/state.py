"""不依赖外部实现的 Runtime v4 Agent 状态机。

本模块同时容纳两套状态机，属阶段迁移期的临时共存：

* 旧状态机（``AgentPhase`` / ``AgentState``）：仍被未迁移的 engine / execution 使用，
  调用方清零后在后续阶段物理删除。
* 新状态机（``AgentRunState`` + 判别联合 ``AgentRunEvent`` + 模块级 ``transition()``）：
  是《AgentRun 状态机总体设计》的目标契约，本次阶段 0 冻结其纯领域行为，
  后续阶段将引擎、持久化与恢复入口迁到它，最终删除旧状态机。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from .control import AgentAction
from .events import (
    AbandonRequested,
    ApprovalGranted,
    ApprovalRejected,
    CancelRequested,
    DelegationCompleted,
    DelegationRequested,
    DelegationSubmitted,
    LLMCallFailed,
    LLMResponseProduced,
    RunStarted,
    TimeoutReached,
    ToolApprovalRequired,
    ToolBatchCompleted,
    ToolBatchFailed,
)


# ============================================================================
# 旧状态机（迁移期临时共存，调用方清零后删除）
# ============================================================================


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

    def transition(self, event: object) -> StateTransition:
        """根据领域事件返回新的状态与下一项执行动作。"""
        from .events import (
            ApprovalResolved,
            CancelRequested,
            DelegationCompleted,
            DelegationSubmitted,
            LLMCompleted,
            RunStarted,
            TimeoutReached,
            ToolCompleted,
        )

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

    def _on_llm_completed(self, event: object) -> StateTransition:
        from .events import LLMCompletionKind

        self._require_phase(AgentPhase.WAITING_LLM)
        if event.kind is LLMCompletionKind.FINAL_RESPONSE:
            return StateTransition(self._with_phase(AgentPhase.FINALIZING), AgentAction.FINALIZE)
        if event.kind is LLMCompletionKind.TOOL_CALLS:
            return StateTransition(self._with_phase(AgentPhase.WAITING_TOOLS), AgentAction.EXECUTE_TOOLS)
        return StateTransition(self._with_phase(AgentPhase.FAILED), AgentAction.FINALIZE)

    def _on_tool_completed(self, event: object) -> StateTransition:
        from .events import ToolCompletionKind

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

    def _on_approval_resolved(self, event: object) -> StateTransition:
        from .events import ApprovalResolved

        self._require_phase(AgentPhase.WAITING_APPROVAL)
        if event.approval_id != self.waiting_control_id:
            raise RuntimeError("审批事件不属于当前等待控制项")
        if event.approved:
            next_state: AgentState = self._with_phase(AgentPhase.WAITING_TOOLS)
            return StateTransition(next_state, AgentAction.EXECUTE_TOOLS)
        return StateTransition(self._with_phase(AgentPhase.CANCELLED), AgentAction.FINALIZE)

    def _on_delegation_submitted(self, event: object) -> StateTransition:
        """进入等待子运行结果的状态，由 Engine 继续查询 DelegationPort。"""
        self._require_phase(AgentPhase.WAITING_TOOLS)
        return StateTransition(self._with_phase(AgentPhase.WAITING_DELEGATION), AgentAction.WAIT)

    def _on_delegation_completed(self, event: object) -> StateTransition:
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
    """状态机处理一个领域事件后的结果。

    迁移期内 ``state`` 同时承载旧 ``AgentState`` 与新 ``AgentRunState``；两种状态机
    均通过本容器返回「下一状态 + 下一项动作」，调用方清零旧类型后收敛为仅 ``AgentRunState``。
    """

    state: AgentState | AgentRunState
    action: AgentAction


# ============================================================================
# 新状态机（目标契约）
# ============================================================================


class RunStage(StrEnum):
    """运行进入具体执行阶段后的活动子状态。"""

    PREPARING = "preparing"
    CALLING_LLM = "calling_llm"
    EXECUTING_TOOLS = "executing_tools"


class SuspendReason(StrEnum):
    """运行被挂起等待外部输入的语义原因。"""

    APPROVAL = "approval"
    DELEGATION = "delegation"


class RunOutcome(StrEnum):
    """运行终态的业务结果类别。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Created:
    """运行已被持久化但尚未开始；不含业务字段。"""


@dataclass(frozen=True)
class Running:
    """运行正在执行，``stage`` 表示当前活动子阶段。"""

    stage: RunStage


@dataclass(frozen=True)
class Suspended:
    """运行挂起等待外部输入；``control_id`` 仅用于校验唤醒事件归属当前等待项。"""

    reason: SuspendReason
    control_id: str
    resume_stage: RunStage


@dataclass(frozen=True)
class Ended:
    """运行终态根类型；``outcome`` 区分成功与非成功结果。"""

    outcome: RunOutcome


# 联合状态：同一时刻只能为四种分支之一，避免生命周期、阶段、等待原因、结果的非法笛卡尔积。
RunMode: TypeAlias = Created | Running | Suspended | Ended


@dataclass(frozen=True)
class AgentRunState:
    """单个 AgentRun 唯一的持久化控制状态。

    仅持有当前状态分支与累计控制/统计值；进入 ``Ended`` 后保留最终值但不再修改。
    ``is_ended()`` 是仓储筛选、Session 占用判断和入口结果判断的唯一标准。
    """

    mode: RunMode
    iteration: int = 0
    retry_count: int = 0
    truncate_count: int = 0
    loop_fingerprint: str = ""

    def is_ended(self) -> bool:
        """判断运行是否已进入终态。"""
        return isinstance(self.mode, Ended)

    def is_active(self) -> bool:
        """判断运行是否尚未结束，可继续接受事件。"""
        return not self.is_ended()

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典（含四模式判别与累计统计）。"""
        return {
            "mode": _mode_to_dict(self.mode),
            "iteration": self.iteration,
            "retry_count": self.retry_count,
            "truncate_count": self.truncate_count,
            "loop_fingerprint": self.loop_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AgentRunState:
        """从 JSON 兼容字典恢复；拒绝非 v4 状态格式以避免隐式迁移。"""
        if not isinstance(data, dict):
            raise ValueError("AgentRunState 必须是对象")
        mode: RunMode = _mode_from_dict(data.get("mode"))
        return cls(
            mode=mode,
            iteration=_as_int(data.get("iteration")),
            retry_count=_as_int(data.get("retry_count")),
            truncate_count=_as_int(data.get("truncate_count")),
            loop_fingerprint=_as_str(data.get("loop_fingerprint")),
        )


def _mode_to_dict(mode: RunMode) -> dict[str, object]:
    """四模式分别序列化为带 type 判别的字典。"""
    if isinstance(mode, Created):
        return {"type": "created"}
    if isinstance(mode, Running):
        return {"type": "running", "stage": mode.stage.value}
    if isinstance(mode, Suspended):
        return {
            "type": "suspended",
            "reason": mode.reason.value,
            "control_id": mode.control_id,
            "resume_stage": mode.resume_stage.value,
        }
    return {"type": "ended", "outcome": mode.outcome.value}


def _mode_from_dict(value: object) -> RunMode:
    """从判别字典恢复四模式之一。"""
    if not isinstance(value, dict):
        raise ValueError("AgentRunState.mode 必须是对象")
    kind: object = value.get("type")
    if kind == "created":
        return Created()
    if kind == "running":
        return Running(RunStage(_as_str(value.get("stage"), "calling_llm")))
    if kind == "suspended":
        return Suspended(
            SuspendReason(_as_str(value.get("reason"), "approval")),
            _as_str(value.get("control_id")),
            RunStage(_as_str(value.get("resume_stage"), "calling_llm")),
        )
    if kind == "ended":
        return Ended(RunOutcome(_as_str(value.get("outcome"), "completed")))
    raise ValueError(f"未知 AgentRunState 模式：{kind}")


def _as_int(value: object, default: int = 0) -> int:
    """从 JSON 值收窄为整数，拒绝布尔。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_str(value: object, default: str = "") -> str:
    """从 JSON 值收窄为字符串。"""
    return value if isinstance(value, str) else default


class InvalidTransition(Exception):
    """状态机拒绝非法迁移时抛出；不修改任何状态。

    携带 ``current_mode`` / ``event_type`` / ``reason`` 供 Application 边界追加
    ``STATE_TRANSITION_REJECTED`` 审计事实，且不包含敏感负载。
    """

    def __init__(self, message: str, *, current_mode: str, event_type: str, reason: str) -> None:
        """保存审计所需的稳定引用与安全原因。"""
        super().__init__(message)
        self.current_mode: str = current_mode
        self.event_type: str = event_type
        self.reason: str = reason


def transition(state: AgentRunState, event: AgentRunEvent) -> StateTransition:
    """按联合状态与事件计算下一状态与下一项动作；非法输入抛 ``InvalidTransition``。

    状态机只计算状态与动作，不构造 LLM 请求、ToolCall、审批记录、checkpoint 或审计。
    取消、超时与放弃适用于任意未结束状态；已结束状态收到任何事件均拒绝。
    """
    if isinstance(event, CancelRequested):
        return _finalize_if_active(state, RunOutcome.CANCELLED, "cancel_requested")
    if isinstance(event, TimeoutReached):
        return _finalize_if_active(state, RunOutcome.FAILED, "timeout_reached")
    if isinstance(event, AbandonRequested):
        return _finalize_if_active(state, RunOutcome.ABANDONED, "abandon_requested")

    match state.mode:
        case Created():
            return _on_created(state, event)
        case Running():
            return _on_running(state, event)
        case Suspended():
            return _on_suspended(state, event)
        case Ended():
            raise InvalidTransition(
                "已结束的运行不再接受事件",
                current_mode=_mode_name(state.mode),
                event_type=type(event).__name__,
                reason="run_already_ended",
            )


def _finalize_if_active(state: AgentRunState, outcome: RunOutcome, reason: str) -> StateTransition:
    """取消/超时/放弃：未结束运行收口为对应终态，已结束运行拒绝。"""
    if state.is_ended():
        raise InvalidTransition(
            "已结束的运行不再接受控制事件",
            current_mode=_mode_name(state.mode),
            event_type=reason,
            reason="run_already_ended",
        )
    return StateTransition(_end(state, outcome), AgentAction.FINALIZE)


def _on_created(state: AgentRunState, event: AgentRunEvent) -> StateTransition:
    """Created 仅接受 RunStarted，进入首个实际阶段 CALLING_LLM。"""
    if isinstance(event, RunStarted):
        return StateTransition(
            AgentRunState(
                mode=Running(RunStage.CALLING_LLM),
                iteration=1,
                retry_count=state.retry_count,
                truncate_count=state.truncate_count,
                loop_fingerprint=state.loop_fingerprint,
            ),
            AgentAction.INVOKE_LLM,
        )
    raise InvalidTransition(
        "Created 状态仅接受 RunStarted",
        current_mode=_mode_name(state.mode),
        event_type=type(event).__name__,
        reason="created_expects_run_started",
    )


def _on_running(state: AgentRunState, event: AgentRunEvent) -> StateTransition:
    """Running 按当前 stage 匹配有限个合法事件。"""
    running: Running = state.mode
    if running.stage is RunStage.CALLING_LLM:
        if isinstance(event, LLMResponseProduced):
            if event.final:
                return StateTransition(_end(state, RunOutcome.COMPLETED), AgentAction.FINALIZE)
            return StateTransition(
                AgentRunState(
                    mode=Running(RunStage.EXECUTING_TOOLS),
                    iteration=state.iteration,
                    retry_count=state.retry_count,
                    truncate_count=state.truncate_count,
                    loop_fingerprint=state.loop_fingerprint,
                ),
                AgentAction.EXECUTE_TOOLS,
            )
        if isinstance(event, LLMCallFailed):
            return StateTransition(_end(state, RunOutcome.FAILED), AgentAction.FINALIZE)
        raise InvalidTransition(
            "CALLING_LLM 只接受 LLM 响应或失败事件",
            current_mode=_mode_name(state.mode),
            event_type=type(event).__name__,
            reason="calling_llm_expects_llm_event",
        )
    if running.stage is RunStage.EXECUTING_TOOLS:
        if isinstance(event, ToolBatchCompleted):
            return StateTransition(
                AgentRunState(
                    mode=Running(RunStage.CALLING_LLM),
                    iteration=state.iteration + 1,
                    retry_count=state.retry_count,
                    truncate_count=state.truncate_count,
                    loop_fingerprint=state.loop_fingerprint,
                ),
                AgentAction.INVOKE_LLM,
            )
        if isinstance(event, ToolApprovalRequired):
            return StateTransition(
                AgentRunState(
                    mode=Suspended(SuspendReason.APPROVAL, event.approval_id, RunStage.EXECUTING_TOOLS),
                    iteration=state.iteration,
                    retry_count=state.retry_count,
                    truncate_count=state.truncate_count,
                    loop_fingerprint=state.loop_fingerprint,
                ),
                AgentAction.SUSPEND,
            )
        if isinstance(event, ToolBatchFailed):
            return StateTransition(_end(state, RunOutcome.FAILED), AgentAction.FINALIZE)
        if isinstance(event, DelegationRequested):
            # 状态不变，交由 Engine 执行 HANDOFF_TARGET 提交子运行。
            return StateTransition(state, AgentAction.HANDOFF_TARGET)
        if isinstance(event, DelegationSubmitted):
            return StateTransition(
                AgentRunState(
                    mode=Suspended(SuspendReason.DELEGATION, event.child_run_id, RunStage.CALLING_LLM),
                    iteration=state.iteration,
                    retry_count=state.retry_count,
                    truncate_count=state.truncate_count,
                    loop_fingerprint=state.loop_fingerprint,
                ),
                AgentAction.SUSPEND,
            )
        raise InvalidTransition(
            "EXECUTING_TOOLS 只接受工具批次或 delegation 事件",
            current_mode=_mode_name(state.mode),
            event_type=type(event).__name__,
            reason="executing_tools_expects_tool_event",
        )
    # PREPARING 等预留阶段当前未激活，不接受任何事件。
    raise InvalidTransition(
        "预留阶段尚未激活",
        current_mode=_mode_name(state.mode),
        event_type=type(event).__name__,
        reason="preparing_stage_not_active",
    )


def _on_suspended(state: AgentRunState, event: AgentRunEvent) -> StateTransition:
    """Suspended 按挂起原因匹配唤醒事件，并校验 control_id 归属。"""
    suspended: Suspended = state.mode
    if suspended.reason is SuspendReason.APPROVAL:
        if isinstance(event, ApprovalGranted):
            if event.approval_id != suspended.control_id:
                raise InvalidTransition(
                    "审批标识不匹配当前等待项",
                    current_mode=_mode_name(state.mode),
                    event_type=type(event).__name__,
                    reason="approval_id_mismatch",
                )
            return StateTransition(
                AgentRunState(
                    mode=Running(RunStage.EXECUTING_TOOLS),
                    iteration=state.iteration,
                    retry_count=state.retry_count,
                    truncate_count=state.truncate_count,
                    loop_fingerprint=state.loop_fingerprint,
                ),
                AgentAction.EXECUTE_TOOLS,
            )
        if isinstance(event, ApprovalRejected):
            if event.approval_id != suspended.control_id:
                raise InvalidTransition(
                    "审批标识不匹配当前等待项",
                    current_mode=_mode_name(state.mode),
                    event_type=type(event).__name__,
                    reason="approval_id_mismatch",
                )
            return StateTransition(_end(state, RunOutcome.CANCELLED), AgentAction.FINALIZE)
        raise InvalidTransition(
            "APPROVAL 挂起仅接受审批结果事件",
            current_mode=_mode_name(state.mode),
            event_type=type(event).__name__,
            reason="suspended_approval_expects_approval_event",
        )
    if suspended.reason is SuspendReason.DELEGATION:
        if isinstance(event, DelegationCompleted):
            if event.child_run_id != suspended.control_id:
                raise InvalidTransition(
                    "子运行标识不匹配当前等待项",
                    current_mode=_mode_name(state.mode),
                    event_type=type(event).__name__,
                    reason="child_run_id_mismatch",
                )
            return StateTransition(
                AgentRunState(
                    mode=Running(RunStage.CALLING_LLM),
                    iteration=state.iteration + 1,
                    retry_count=state.retry_count,
                    truncate_count=state.truncate_count,
                    loop_fingerprint=state.loop_fingerprint,
                ),
                AgentAction.INVOKE_LLM,
            )
        raise InvalidTransition(
            "DELEGATION 挂起仅接受子运行完成事件",
            current_mode=_mode_name(state.mode),
            event_type=type(event).__name__,
            reason="suspended_delegation_expects_delegation_completed",
        )
    raise InvalidTransition(
        "未知挂起原因",
        current_mode=_mode_name(state.mode),
        event_type=type(event).__name__,
        reason="unknown_suspend_reason",
    )


def _end(state: AgentRunState, outcome: RunOutcome) -> AgentRunState:
    """保留累计统计值，收口为 Ended(outcome)。"""
    return AgentRunState(
        mode=Ended(outcome),
        iteration=state.iteration,
        retry_count=state.retry_count,
        truncate_count=state.truncate_count,
        loop_fingerprint=state.loop_fingerprint,
    )


def _mode_name(mode: RunMode) -> str:
    """返回供审计使用的稳定 mode 字符串。"""
    if isinstance(mode, Created):
        return "created"
    if isinstance(mode, Running):
        return f"running:{mode.stage.value}"
    if isinstance(mode, Suspended):
        return f"suspended:{mode.reason.value}"
    return f"ended:{mode.outcome.value}"
