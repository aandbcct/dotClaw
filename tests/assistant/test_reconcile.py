from types import SimpleNamespace

import pytest

from dotclaw.assistant.schemas import BatchPrepareInput, TaskCreateInput
from dotclaw.runtime.domain.facts import ToolCall
from dotclaw.runtime.domain.tool_recovery import ToolCallKind, ToolCallLedger, ToolCallRecord, ToolCallState

from .test_service import add_user_run, make_context, make_service


@pytest.mark.asyncio
async def test_reconcile_keeps_unsettled_uncertainty_then_closes_rejected_approval(tmp_path):
    service, _, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    first = await service.create_task(TaskCreateInput(title="待处理一"), make_context(key="task-1"))
    second = await service.create_task(TaskCreateInput(title="待处理二"), make_context(call_id="task-2", key="task-2"))
    add_user_run(run_repository, run_id="approval-run", message_id="approval-message")
    prepared = await service.prepare_batch(
        BatchPrepareInput(summary="等待批准", items=[
            {"action": "update", "task_id": first.task_refs[0].id,
             "expected_version": first.task_refs[0].version, "patch": {"title": "新一"}},
            {"action": "update", "task_id": second.task_refs[0].id,
             "expected_version": second.task_refs[0].version, "patch": {"title": "新二"}},
        ]),
        make_context(run_id="approval-run", call_id="prepare", key="prepare", message_id="approval-message"),
    )
    request = prepared.data.request
    run_repository.runs["approval-run"].state = SimpleNamespace(is_ended=lambda: True)
    call = ToolCall(
        call_id="apply-call",
        name="builtin.assistant.batch.apply",
        arguments={"request_id": request.id, "expected_version": request.version, "proposal_hash": request.proposal_hash},
    )
    uncertain = ToolCallRecord(
        run_id="approval-run",
        call=call,
        source_message_id="assistant-message",
        ordinal=0,
        kind=ToolCallKind.ORDINARY,
        state=ToolCallState.UNCERTAIN,
        uncertainty_reason="调用结果尚未确定",
    )
    run_repository.ledgers["approval-run"] = ToolCallLedger(
        session_id="personal-assistant", run_id="approval-run", revision=1, calls=(uncertain,)
    )
    await service.reconcile_controls("personal-assistant")
    still_waiting = await service._repository.get_request("personal-assistant", request.id)
    assert still_waiting.status == "awaiting_confirmation"
    assert still_waiting.proposal is not None

    rejected = ToolCallRecord(
        run_id="approval-run",
        call=call,
        source_message_id="assistant-message",
        ordinal=0,
        kind=ToolCallKind.ORDINARY,
        state=ToolCallState.PENDING,
        approval_id="approval-1",
        approval_decision=False,
    )
    run_repository.ledgers["approval-run"] = ToolCallLedger(
        session_id="personal-assistant", run_id="approval-run", revision=2, calls=(rejected,)
    )
    await service.reconcile_controls("personal-assistant")
    closed = await service._repository.get_request("personal-assistant", request.id)
    assert closed.status == "awaiting_input"
    assert closed.proposal is None
    assert "审批被拒绝" in closed.payload.last_error
