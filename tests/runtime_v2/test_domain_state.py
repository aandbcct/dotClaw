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
from dotclaw.runtime.domain.models import (
    AgentAction,
    AgentPolicySnapshot,
    ConversationMessage,
    ConversationSnapshot,
    MessageRole,
    RunRequest,
)
from dotclaw.runtime.domain.state import AgentPhase, AgentState


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
