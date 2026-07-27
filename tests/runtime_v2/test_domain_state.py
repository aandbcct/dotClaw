"""Runtime v2 纯领域状态机与序列化契约测试。"""

from __future__ import annotations

import json

import pytest

from dotclaw.runtime.domain.events import (
    ApprovalResolved,
    CancelRequested,
    LLMCompleted,
    LLMCompletionKind,
    RunEvent,
    RunEventType,
    RunStarted,
    ToolCompleted,
    ToolCompletionKind,
)
from dotclaw.runtime.application.execution import RunBudget, RunExecution
from dotclaw.runtime.application.dto import (
    ConversationMessage,
    ConversationSnapshot,
    RunRequest,
)
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.events import (
    AbandonRequested,
    ApprovalGranted,
    ApprovalRejected,
    CancelRequested,
    DelegationCompleted,
    DelegationRequested,
    DelegationSubmitted,
    LLMCallFailed,
    LLMResponseProduced,
    RunEventType,
    RunStarted,
    TimeoutReached,
    ToolApprovalRequired,
    ToolBatchCompleted,
    ToolBatchFailed,
)
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    MessageRole,
)
from dotclaw.runtime.domain.state import (
    AgentPhase,
    AgentRunState,
    AgentState,
    Created,
    Ended,
    InvalidTransition,
    RunOutcome,
    RunStage,
    Suspended,
    SuspendReason,
    Running,
    transition,
)


def _build_request() -> RunRequest:
    """构造冻结的最小普通用户请求。"""
    user_message: ConversationMessage = ConversationMessage(
        message_id="message-user-1",
        role=MessageRole.USER,
        content="请处理这个请求",
        created_at="2026-07-16T00:00:00+00:00",
    )
    conversation: ConversationSnapshot = ConversationSnapshot(
        session_id="session-1",
        messages=(user_message,),
        version=1,
    )
    return RunRequest(
        session_id="session-1",
        lease_id="lease-1",
        agent_id="agent-1",
        user_message=user_message,
        conversation=conversation,
    )


def _build_policy() -> AgentPolicySnapshot:
    """构造运行期间不可变的执行策略。"""
    return AgentPolicySnapshot(
        agent_id="agent-1",
        identity_version="identity-v1",
        model_id="model-v1",
        max_iterations=8,
    )


def test_state_machine_completes_tool_then_final_response() -> None:
    """状态机仅通过领域事件完成 Think-Act-Think 主流程。"""
    initial_state: AgentState = AgentState()

    started_transition = initial_state.transition(RunStarted("message-user-1"))
    tool_transition = started_transition.state.transition(
        LLMCompleted(LLMCompletionKind.TOOL_CALLS, tool_call_count=1),
    )
    tool_done_transition = tool_transition.state.transition(
        ToolCompleted(ToolCompletionKind.COMPLETED, ("message-tool-1",)),
    )
    final_transition = tool_done_transition.state.transition(
        LLMCompleted(LLMCompletionKind.FINAL_RESPONSE, "message-assistant-1"),
    )

    assert started_transition.action is AgentAction.INVOKE_LLM
    assert tool_transition.action is AgentAction.EXECUTE_TOOLS
    assert tool_done_transition.action is AgentAction.INVOKE_LLM
    assert tool_done_transition.state.iteration == 2
    assert final_transition.action is AgentAction.FINALIZE
    assert final_transition.state.phase is AgentPhase.FINALIZING


def test_state_machine_waits_for_matching_approval() -> None:
    """审批恢复必须匹配正在等待的审批标识。"""
    started_state: AgentState = AgentState().transition(RunStarted("message-user-1")).state
    tool_state: AgentState = started_state.transition(
        LLMCompleted(LLMCompletionKind.TOOL_CALLS, tool_call_count=1),
    ).state
    waiting_transition = tool_state.transition(
        ToolCompleted(ToolCompletionKind.APPROVAL_REQUIRED, approval_id="approval-1"),
    )

    assert waiting_transition.action is AgentAction.WAIT
    assert waiting_transition.state.phase is AgentPhase.WAITING_APPROVAL
    with pytest.raises(RuntimeError, match="不属于"):
        waiting_transition.state.transition(ApprovalResolved("approval-other", True))

    resumed_transition = waiting_transition.state.transition(ApprovalResolved("approval-1", True))
    assert resumed_transition.action is AgentAction.EXECUTE_TOOLS
    assert resumed_transition.state.phase is AgentPhase.WAITING_TOOLS


def test_cancel_event_finishes_from_any_safe_phase() -> None:
    """取消事件无需了解外部实现即可结束运行。"""
    waiting_state: AgentState = AgentState().transition(RunStarted("message-user-1")).state
    cancelled_transition = waiting_state.transition(CancelRequested("用户取消"))

    assert cancelled_transition.action is AgentAction.FINALIZE
    assert cancelled_transition.state.phase is AgentPhase.CANCELLED
    assert cancelled_transition.state.is_terminal()


def test_domain_models_are_json_serializable() -> None:
    """RunRequest、RunExecution 与 RunEvent 均可序列化为 JSON。"""
    request: RunRequest = _build_request()
    execution: RunExecution = RunExecution(
        run_id="run-1",
        request=request,
        policy=_build_policy(),
        state=AgentState(),
        budget=RunBudget(max_iterations=8, timeout_ms=30_000),
    )
    event: RunEvent = RunEvent(
        run_id="run-1",
        sequence=1,
        event_type=RunEventType.RUN_STARTED,
        occurred_at="2026-07-16T00:00:00+00:00",
        message_ids=("message-user-1",),
    )

    request_json: str = json.dumps(request.to_dict(), ensure_ascii=False)
    execution_json: str = json.dumps(execution.to_dict(), ensure_ascii=False)
    event_json: str = json.dumps(event.to_dict(), ensure_ascii=False)

    assert "session-1" in request_json
    assert "run-1" in execution_json
    assert "run_started" in event_json


# ============================================================================
# 新状态机（阶段 0 契约）迁移矩阵与非法事件测试
# ============================================================================

def _created() -> AgentRunState:
    """构造未开始的 Created 状态。"""
    return AgentRunState(mode=Created())


def _running_calling_llm() -> AgentRunState:
    """Created + RunStarted 后的 Running(CALLING_LLM)。"""
    return transition(_created(), RunStarted("message-user-1")).state


def _running_executing_tools() -> AgentRunState:
    """Running(CALLING_LLM) + 工具调用响应后的 Running(EXECUTING_TOOLS)。"""
    return transition(
        _running_calling_llm(),
        LLMResponseProduced(final=False, tool_call_count=1),
    ).state


def _suspended_approval() -> AgentRunState:
    """Running(EXECUTING_TOOLS) + 需要审批后的 Suspended(APPROVAL)。"""
    return transition(
        _running_executing_tools(),
        ToolApprovalRequired(approval_id="approval-1"),
    ).state


def _suspended_delegation() -> AgentRunState:
    """Running(EXECUTING_TOOLS) + 提交子运行后的 Suspended(DELEGATION)。"""
    return transition(
        _running_executing_tools(),
        DelegationSubmitted(child_run_id="child-1"),
    ).state


def test_matrix_created_to_running_calling_llm() -> None:
    """Created + RunStarted -> Running(CALLING_LLM) + INVOKE_LLM。"""
    result = transition(_created(), RunStarted("message-user-1"))
    assert isinstance(result.state.mode, Running)
    assert result.state.mode.stage is RunStage.CALLING_LLM
    assert result.state.iteration == 1
    assert result.action is AgentAction.INVOKE_LLM


def test_matrix_calling_llm_final_response_completes() -> None:
    """Running(CALLING_LLM) + LLMResponseProduced(final) -> Ended(COMPLETED) + FINALIZE。"""
    result = transition(_running_calling_llm(), LLMResponseProduced(final=True))
    assert isinstance(result.state.mode, Ended)
    assert result.state.mode.outcome is RunOutcome.COMPLETED
    assert result.state.is_ended()
    assert result.action is AgentAction.FINALIZE


def test_matrix_calling_llm_tool_calls_to_executing() -> None:
    """Running(CALLING_LLM) + LLMResponseProduced(tool_calls) -> Running(EXECUTING_TOOLS) + EXECUTE_TOOLS。"""
    result = transition(
        _running_calling_llm(),
        LLMResponseProduced(final=False, tool_call_count=2),
    )
    assert isinstance(result.state.mode, Running)
    assert result.state.mode.stage is RunStage.EXECUTING_TOOLS
    assert result.action is AgentAction.EXECUTE_TOOLS


def test_matrix_calling_llm_failed() -> None:
    """Running(CALLING_LLM) + LLMCallFailed -> Ended(FAILED) + FINALIZE。"""
    result = transition(_running_calling_llm(), LLMCallFailed())
    assert isinstance(result.state.mode, Ended)
    assert result.state.mode.outcome is RunOutcome.FAILED
    assert result.action is AgentAction.FINALIZE


def test_matrix_executing_tools_completed_back_to_llm() -> None:
    """Running(EXECUTING_TOOLS) + ToolBatchCompleted -> Running(CALLING_LLM) + INVOKE_LLM，迭代 +1。"""
    result = transition(_running_executing_tools(), ToolBatchCompleted(result_message_ids=("m1",)))
    assert isinstance(result.state.mode, Running)
    assert result.state.mode.stage is RunStage.CALLING_LLM
    assert result.state.iteration == 2
    assert result.action is AgentAction.INVOKE_LLM


def test_matrix_executing_tools_approval_required() -> None:
    """Running(EXECUTING_TOOLS) + ToolApprovalRequired -> Suspended(APPROVAL) + SUSPEND。"""
    result = transition(
        _running_executing_tools(),
        ToolApprovalRequired(approval_id="approval-1"),
    )
    assert isinstance(result.state.mode, Suspended)
    assert result.state.mode.reason is SuspendReason.APPROVAL
    assert result.state.mode.control_id == "approval-1"
    assert result.state.mode.resume_stage is RunStage.EXECUTING_TOOLS
    assert result.action is AgentAction.SUSPEND


def test_matrix_executing_tools_failed() -> None:
    """Running(EXECUTING_TOOLS) + ToolBatchFailed -> Ended(FAILED) + FINALIZE。"""
    result = transition(_running_executing_tools(), ToolBatchFailed())
    assert isinstance(result.state.mode, Ended)
    assert result.state.mode.outcome is RunOutcome.FAILED
    assert result.action is AgentAction.FINALIZE


def test_matrix_executing_tools_delegation_requested_state_unchanged() -> None:
    """Running(EXECUTING_TOOLS) + DelegationRequested -> 状态不变 + HANDOFF_TARGET。"""
    state = _running_executing_tools()
    result = transition(state, DelegationRequested())
    assert result.state is state
    assert result.action is AgentAction.HANDOFF_TARGET


def test_matrix_executing_tools_delegation_submitted() -> None:
    """Running(EXECUTING_TOOLS) + DelegationSubmitted -> Suspended(DELEGATION) + SUSPEND。"""
    result = transition(
        _running_executing_tools(),
        DelegationSubmitted(child_run_id="child-1"),
    )
    assert isinstance(result.state.mode, Suspended)
    assert result.state.mode.reason is SuspendReason.DELEGATION
    assert result.state.mode.control_id == "child-1"
    assert result.state.mode.resume_stage is RunStage.CALLING_LLM
    assert result.action is AgentAction.SUSPEND


def test_matrix_suspended_approval_granted() -> None:
    """Suspended(APPROVAL) + ApprovalGranted -> Running(EXECUTING_TOOLS) + EXECUTE_TOOLS。"""
    result = transition(_suspended_approval(), ApprovalGranted(approval_id="approval-1"))
    assert isinstance(result.state.mode, Running)
    assert result.state.mode.stage is RunStage.EXECUTING_TOOLS
    assert result.action is AgentAction.EXECUTE_TOOLS


def test_matrix_suspended_approval_rejected() -> None:
    """Suspended(APPROVAL) + ApprovalRejected -> Ended(CANCELLED) + FINALIZE。"""
    result = transition(_suspended_approval(), ApprovalRejected(approval_id="approval-1"))
    assert isinstance(result.state.mode, Ended)
    assert result.state.mode.outcome is RunOutcome.CANCELLED
    assert result.action is AgentAction.FINALIZE


def test_matrix_suspended_delegation_completed() -> None:
    """Suspended(DELEGATION) + DelegationCompleted -> Running(CALLING_LLM) + INVOKE_LLM，迭代 +1。"""
    result = transition(
        _suspended_delegation(),
        DelegationCompleted(child_run_id="child-1", succeeded=True, result_message_id="m-result"),
    )
    assert isinstance(result.state.mode, Running)
    assert result.state.mode.stage is RunStage.CALLING_LLM
    assert result.state.iteration == 2
    assert result.action is AgentAction.INVOKE_LLM


def test_matrix_control_cancel_from_any_active_state() -> None:
    """任意未结束状态 + CancelRequested -> Ended(CANCELLED) + FINALIZE。"""
    for state in (_created(), _running_calling_llm(), _suspended_approval()):
        result = transition(state, CancelRequested("用户取消"))
        assert isinstance(result.state.mode, Ended)
        assert result.state.mode.outcome is RunOutcome.CANCELLED
        assert result.action is AgentAction.FINALIZE


def test_matrix_control_timeout_from_any_active_state() -> None:
    """任意未结束状态 + TimeoutReached -> Ended(FAILED) + FINALIZE。"""
    result = transition(_running_executing_tools(), TimeoutReached(timeout_ms=1000))
    assert isinstance(result.state.mode, Ended)
    assert result.state.mode.outcome is RunOutcome.FAILED
    assert result.action is AgentAction.FINALIZE


def test_matrix_control_abandon_from_created() -> None:
    """Created + AbandonRequested -> Ended(ABANDONED) + FINALIZE。"""
    result = transition(_created(), AbandonRequested(reason="主动放弃"))
    assert isinstance(result.state.mode, Ended)
    assert result.state.mode.outcome is RunOutcome.ABANDONED
    assert result.action is AgentAction.FINALIZE


def test_invalid_wrong_approval_id() -> None:
    """审批标识不匹配当前等待项必须抛 InvalidTransition 且状态不变。"""
    state = _suspended_approval()
    with pytest.raises(InvalidTransition) as exc_info:
        transition(state, ApprovalGranted(approval_id="approval-other"))
    assert exc_info.value.reason == "approval_id_mismatch"
    assert exc_info.value.current_mode == "suspended:approval"
    assert state.mode.reason is SuspendReason.APPROVAL


def test_invalid_wrong_child_run_id() -> None:
    """子运行标识不匹配当前等待项必须抛 InvalidTransition 且状态不变。"""
    state = _suspended_delegation()
    with pytest.raises(InvalidTransition) as exc_info:
        transition(state, DelegationCompleted(child_run_id="child-other", succeeded=True))
    assert exc_info.value.reason == "child_run_id_mismatch"
    assert exc_info.value.current_mode == "suspended:delegation"
    assert state.mode.control_id == "child-1"


def test_invalid_ended_rejects_any_event() -> None:
    """已结束状态收到任何事件都必须抛 InvalidTransition。"""
    ended = transition(_running_calling_llm(), LLMResponseProduced(final=True)).state
    assert ended.is_ended()
    for event in (
        RunStarted("m"),
        LLMResponseProduced(final=False),
        ToolBatchCompleted(),
        CancelRequested("x"),
        AbandonRequested(),
    ):
        with pytest.raises(InvalidTransition) as exc_info:
            transition(ended, event)
        assert exc_info.value.reason == "run_already_ended"


def test_invalid_unexpected_event_in_calling_llm() -> None:
    """CALLING_LLM 收到工具类事件必须抛 InvalidTransition。"""
    with pytest.raises(InvalidTransition) as exc_info:
        transition(_running_calling_llm(), ToolBatchCompleted())
    assert exc_info.value.reason == "calling_llm_expects_llm_event"


def test_invalid_duplicate_run_started_rejected() -> None:
    """Running 阶段再次收到 RunStarted 必须抛 InvalidTransition（重复/错序事件）。"""
    with pytest.raises(InvalidTransition) as exc_info:
        transition(_running_calling_llm(), RunStarted("message-user-1"))
    assert exc_info.value.reason == "calling_llm_expects_llm_event"


def test_state_snapshot_is_immutable() -> None:
    """一次 transition 不得修改输入 state；改变状态时返回新对象，状态不变时复用原对象。"""
    original = _running_executing_tools()
    original_mode = original.mode
    original_iteration = original.iteration

    # 改变状态：返回新对象，原对象字段保持不动。
    changed = transition(original, ToolBatchCompleted())
    assert changed.state is not original
    assert original.mode is original_mode
    assert original.iteration == original_iteration
    assert not original.is_ended()

    # 状态不变（DelegationRequested）：复用原对象。
    unchanged = transition(original, DelegationRequested())
    assert unchanged.state is original


def test_state_transition_rejected_audit_type_exists() -> None:
    """审计类型包含 STATE_TRANSITION_REJECTED，供 Application 边界记录拒绝迁移。"""
    assert RunEventType.STATE_TRANSITION_REJECTED == "state_transition_rejected"
