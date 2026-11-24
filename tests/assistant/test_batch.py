import pytest

from dotclaw.assistant.schemas import BatchApplyInput, BatchPrepareInput, RequestGetInput, TaskCreateInput, TaskPatch, TaskUpdateInput
from dotclaw.assistant.service import AssistantApprovalRequiredError
from dotclaw.tools.base import ToolExecutionContext

from .test_service import add_user_run, make_context, make_service

@pytest.mark.asyncio
async def test_batch_requires_approval_and_persists_partial_results_for_replay(tmp_path):
    service, task_repository, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    first = await service.create_task(TaskCreateInput(title="待改一"), make_context(key="create-1"))
    second = await service.create_task(TaskCreateInput(title="待改二"), make_context(call_id="create-2", key="create-2"))
    first_task = first.task_refs[0]
    second_task = second.task_refs[0]

    add_user_run(run_repository, run_id="batch-run", message_id="batch-message")
    prepare_context = make_context(run_id="batch-run", call_id="prepare", key="prepare-key", message_id="batch-message")
    prepared = await service.prepare_batch(
        BatchPrepareInput(
            summary="更新两项",
            items=[
                {"action": "update", "task_id": first_task.id, "expected_version": first_task.version,
                 "patch": {"title": "已更新一"}},
                {"action": "update", "task_id": second_task.id, "expected_version": second_task.version,
                 "patch": {"title": "已更新二"}},
            ],
        ),
        prepare_context,
    )
    request = prepared.data.request
    assert request.status == "awaiting_confirmation"
    apply_input = BatchApplyInput(request_id=request.id, expected_version=request.version, proposal_hash=request.proposal_hash)
    apply_context = make_context(run_id="batch-run", call_id="approved-apply", key="apply-key", message_id="batch-message")

    with pytest.raises(AssistantApprovalRequiredError):
        await service.apply_batch(apply_input, apply_context)
    assert (await task_repository.get_detail(first_task.id))[0].title == "待改一"

    wrong_run = ToolExecutionContext(
        agentrun_id="different-run",
        tool_call_id="wrong-run-call",
        idempotency_key="wrong-run-key",
        agent_id="personal-assistant",
        session_id="personal-assistant",
        input_message_id="batch-message",
        input_created_at="2032-04-05T12:00:00+00:00",
        approved=True,
    )
    wrong_run_result = await service.apply_batch(apply_input, wrong_run)
    assert wrong_run_result.outcome == "failed"
    assert wrong_run_result.error.code == "request_conflict"
    assert (await task_repository.get_detail(first_task.id))[0].title == "待改一"

    # 模拟提案冻结后另一条合法业务写入，令第二项在实际应用时版本冲突。
    await service.update_task(
        TaskUpdateInput(task_id=second_task.id, expected_version=second_task.version, patch=TaskPatch(description="并发修改")),
        make_context(run_id="other-run", call_id="other-update", key="other-update"),
    )
    apply_context = ToolExecutionContext(
        agentrun_id="batch-run",
        tool_call_id="approved-apply",
        idempotency_key="apply-key",
        agent_id="personal-assistant",
        session_id="personal-assistant",
        input_message_id="batch-message",
        input_created_at="2032-04-05T12:00:00+00:00",
        approved=True,
    )
    result = await service.apply_batch(apply_input, apply_context)

    assert result.outcome == "partial"
    assert [item.outcome for item in result.items] == ["succeeded", "failed"]
    assert result.items[1].error.code == "version_conflict"
    saved_first = (await task_repository.get_detail(first_task.id))[0]
    saved_second = (await task_repository.get_detail(second_task.id))[0]
    assert saved_first.title == "已更新一"
    assert saved_second.title == "待改二"
    assert saved_second.description == "并发修改"

    saved_request = await service.get_request(
        RequestGetInput(id=request.id),
        make_context(key="read-request"),
    )
    assert saved_request.data.request.status == "awaiting_input"
    replay = await service.apply_batch(apply_input, apply_context)
    assert replay.model_dump(mode="json") == result.model_dump(mode="json")
    assert len(await task_repository.list(None)) == 2
