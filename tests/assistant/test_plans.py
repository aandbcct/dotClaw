import json

import pytest

from dotclaw.assistant.schemas import PlanApplyInput, PlanSaveInput, ReminderCreateInput, RequestSaveInput, TaskCreateInput, TaskUpdateInput
from dotclaw.assistant.models import AssistantInputRef
from dotclaw.assistant.context import AssistantContextReader
from dotclaw.tools.assistant_tools import register_assistant_tools
from dotclaw.tools.registry import ToolRegistry

from .test_service import add_user_run, make_context, make_service


@pytest.mark.asyncio
async def test_plan_save_is_draft_and_apply_requires_current_version(tmp_path):
    service, task_repository, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    event = await service.create_task(
        TaskCreateInput(title="考试", planning={"kind": "event", "event_time": {"kind": "date", "value": "2032-05-01"}, "time_text": "5月1日"}),
        make_context(),
    )
    add_user_run(run_repository, run_id="plan-run", message_id="plan-message")
    saved = await service.save_plan(
        PlanSaveInput(
            summary="考试准备计划", stage="proposed", event_ids=[event.task_refs[0].id], missing_fields=[],
            items=[
                {"title": "复习第一章", "description": "看笔记", "planning": {"kind": "task", "scheduled_time": {"kind": "datetime", "value": "2032-04-30T10:00:00Z"}, "related_task_id": event.task_refs[0].id}},
                {"title": "复习第二章", "deadline": "2032-04-30", "planning": {"kind": "task", "scheduled_time": None, "related_task_id": event.task_refs[0].id}},
            ],
        ),
        make_context(run_id="plan-run", call_id="save", key="save", message_id="plan-message"),
    )
    request = saved.data.request
    assert request.payload.planning_stage == "proposed"
    assert len(await task_repository.list(None)) == 1

    revised = await service.save_plan(
        PlanSaveInput(
            request_id=request.id, expected_version=request.version, summary="考试准备计划调整",
            stage="proposed", event_ids=[event.task_refs[0].id], missing_fields=[],
            items=[
                {"title": "复习第一章", "planning": {"kind": "task", "scheduled_time": {"kind": "datetime", "value": "2032-04-30T10:00:00Z"}, "related_task_id": event.task_refs[0].id}},
                {"title": "复习第二章", "planning": {"kind": "task", "scheduled_time": None, "related_task_id": event.task_refs[0].id}},
            ],
        ),
        make_context(run_id="plan-run", call_id="revise", key="revise", message_id="plan-message"),
    )
    request = revised.data.request

    context_summary = json.loads(await AssistantContextReader(service._repository, service._timezone, False).read(
        "personal-assistant", AssistantInputRef(run_id="plan-run", message_id="plan-message", created_at=service._clock())
    ))
    progress = next(item for item in context_summary["requests"]["items"] if item["id"] == request.id)
    assert progress["planning_stage"] == "proposed"
    assert progress["event_ids"] == [event.task_refs[0].id]
    assert progress["missing_fields"] == []

    stale = await service.apply_plan(
        PlanApplyInput(request_id=request.id, expected_version=request.version - 1),
        make_context(run_id="plan-run", call_id="stale", key="stale", message_id="plan-message"),
    )
    assert stale.outcome == "failed"
    assert len(await task_repository.list(None)) == 1

    add_user_run(run_repository, run_id="accept-run", message_id="accept-message")
    applied_input = PlanApplyInput(request_id=request.id, expected_version=request.version)
    context = make_context(run_id="accept-run", call_id="apply", key="apply", message_id="accept-message")
    applied = await service.apply_plan(applied_input, context)
    assert applied.outcome == "succeeded"
    assert len(applied.task_refs) == 2
    assert len(await task_repository.list(None)) == 3
    assert applied.task_refs[0].planning.scheduled_time.value.isoformat() == "2032-04-30T10:00:00+00:00"
    assert applied.task_refs[0].planning.related_task_id == event.task_refs[0].id
    assert applied.task_refs[1].planning.scheduled_time is None
    assert applied.task_refs[1].planning.related_task_id == event.task_refs[0].id
    resolved = await service._repository.get_request("personal-assistant", request.id)
    assert {(ref.run_id, ref.message_id) for ref in resolved.source_refs} >= {
        ("plan-run", "plan-message"), ("accept-run", "accept-message")
    }
    replay = await service.apply_plan(applied_input, context)
    assert replay.model_dump(mode="json") == applied.model_dump(mode="json")
    assert len(await task_repository.list(None)) == 3


@pytest.mark.asyncio
async def test_plan_apply_rolls_back_all_items_and_keeps_failure_receipt(tmp_path):
    service, task_repository, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    add_user_run(run_repository, run_id="plan-run", message_id="plan-message")
    saved = await service.save_plan(
        PlanSaveInput(
            summary="含无效关联的计划", stage="proposed", items=[
                {"title": "先创建的事务", "planning": {"kind": "task", "scheduled_time": None}},
                {"title": "提醒时间无效", "planning": {"kind": "task", "scheduled_time": None},
                 "reminders": [{"kind": "at", "at": "2032-04-05T11:00:00Z"}]},
            ],
        ),
        make_context(run_id="plan-run", call_id="save", key="save", message_id="plan-message"),
    )
    request = saved.data.request
    apply_input = PlanApplyInput(request_id=request.id, expected_version=request.version)
    context = make_context(run_id="plan-run", call_id="apply", key="apply", message_id="plan-message")
    failed = await service.apply_plan(apply_input, context)
    replay = await service.apply_plan(apply_input, context)
    assert failed.outcome == "failed"
    assert replay.model_dump(mode="json") == failed.model_dump(mode="json")
    assert await task_repository.list(None) == ()


@pytest.mark.asyncio
async def test_proposed_event_plan_rejects_items_without_event_link_at_schema_boundary():
    with pytest.raises(ValueError, match="event_ids"):
        PlanSaveInput(
            summary="未关联事件的计划", stage="proposed", event_ids=["event-id"], missing_fields=[],
            items=[{"title": "准备面试", "planning": {"kind": "task", "scheduled_time": None}}],
        )


@pytest.mark.asyncio
async def test_single_event_update_cannot_resolve_planning_request(tmp_path):
    service, task_repository, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    event = await service.create_task(
        TaskCreateInput(title="面试", planning={"kind": "event", "event_time": {"kind": "date", "value": "2032-05-01"}}),
        make_context(),
    )
    add_user_run(run_repository, run_id="plan-run", message_id="plan-message")
    saved = await service.save_plan(
        PlanSaveInput(
            summary="面试准备草案", stage="drafting", event_ids=[event.task_refs[0].id],
            missing_fields=["准备时间"], items=[],
        ),
        make_context(run_id="plan-run", call_id="save-plan", key="save-plan", message_id="plan-message"),
    )
    request = saved.data.request
    changed = await service.update_task(
        TaskUpdateInput(
            task_id=event.task_refs[0].id,
            expected_version=event.task_refs[0].version,
            request_id=request.id,
            expected_request_version=request.version,
            patch={"planning": {"kind": "event", "event_time": {"kind": "datetime", "value": "2032-05-01T19:00:00+08:00"}}},
        ),
        make_context(run_id="plan-run", call_id="update-event", key="update-event", message_id="plan-message"),
    )
    assert changed.outcome == "failed"
    assert changed.error.code == "invalid_arguments"
    assert "不要传规划 request_id" in changed.error.message
    current_request = await service._repository.get_request("personal-assistant", request.id)
    assert current_request.status == "awaiting_input"
    assert current_request.version == request.version
    current_event, _ = await task_repository.get_detail(event.task_refs[0].id)
    assert current_event.version == event.task_refs[0].version

    stale_plan = await service.save_plan(
        PlanSaveInput(
            request_id=request.id, expected_version=request.version + 1,
            summary="使用错误版本", stage="drafting", event_ids=[event.task_refs[0].id],
            missing_fields=["准备时间"], items=[],
        ),
        make_context(run_id="plan-run", call_id="stale-plan", key="stale-plan", message_id="plan-message"),
    )
    assert stale_plan.outcome == "failed"
    assert stale_plan.error.code == "invalid_arguments"
    assert "requests.get" in stale_plan.error.message
    assert "data.request.version" in stale_plan.error.message

    independent_update = await service.update_task(
        TaskUpdateInput(
            task_id=event.task_refs[0].id, expected_version=event.task_refs[0].version,
            patch={"planning": {"kind": "event", "event_time": {"kind": "datetime", "value": "2032-05-01T19:00:00+08:00"}}},
        ),
        make_context(run_id="plan-run", call_id="independent-update", key="independent-update", message_id="plan-message"),
    )
    assert independent_update.outcome == "succeeded"
    proposed = await service.save_plan(
        PlanSaveInput(
            request_id=request.id, expected_version=request.version, summary="可执行草案", stage="proposed",
            event_ids=[event.task_refs[0].id], missing_fields=[],
            items=[{
                "title": "准备面试", "planning": {
                    "kind": "task", "scheduled_time": {"kind": "datetime", "value": "2032-04-30T18:00:00+08:00"},
                    "related_task_id": event.task_refs[0].id,
                },
            }],
        ),
        make_context(run_id="plan-run", call_id="save-proposed", key="save-proposed", message_id="plan-message"),
    )
    assert proposed.outcome == "succeeded"
    assert proposed.data.request.version == request.version + 1
    assert proposed.data.request.payload.planning_stage == "proposed"
    assert len(await task_repository.list(None)) == 1

    generic_payload = {
        "intent_args": {"action": "create_task", "title": None},
        "missing_fields": ["标题"], "candidate_task_ids": [], "selected_task_ids": [],
        "temporal_values": [], "last_error": None,
    }
    overwrite = await service.save_request(
        RequestSaveInput(
            id=request.id, expected_version=proposed.data.request.version, kind="create_task", summary="替换规划请求",
            payload=generic_payload, source_refs=[],
        ),
        make_context(run_id="plan-run", call_id="overwrite-plan", key="overwrite-plan", message_id="plan-message"),
    )
    create_planning = await service.save_request(
        RequestSaveInput(
            kind="create_task", summary="绕过计划工具", payload={**generic_payload, "planning_stage": "drafting"},
            source_refs=[],
        ),
        make_context(run_id="plan-run", call_id="create-plan-generic", key="create-plan-generic", message_id="plan-message"),
    )
    assert overwrite.outcome == "failed"
    assert create_planning.outcome == "failed"
    assert "plans.save" in overwrite.error.message
    assert "plans.save" in create_planning.error.message
    persisted_request = await service._repository.get_request("personal-assistant", request.id)
    assert persisted_request.status == "awaiting_input"
    assert persisted_request.version == proposed.data.request.version
    assert persisted_request.payload.planning_stage == "proposed"


@pytest.mark.asyncio
async def test_registered_plan_tools_have_strict_identity_and_nested_schemas(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    registry = ToolRegistry()
    register_assistant_tools(registry, service)
    handler = registry.get("builtin.assistant.tasks.create")
    save_handler = registry.get("builtin.assistant.plans.save")
    plan_item_schema = save_handler.definition().parameters["properties"]["items"]["items"]
    assert "planning" in plan_item_schema["required"]
    assert "scheduled_time" in plan_item_schema["properties"]["planning"]["properties"]
    created = await handler.execute(
        {"title": "考试", "planning": {"kind": "event", "event_time": {"kind": "datetime", "value": "2032-05-01T12:00:00Z"}, "time_text": "中午"}},
        make_context(),
    )
    ref = json.loads(created.output)["task_refs"][0]
    assert ref["description"] == ""
    assert ref["planning"]["event_time"] == {"kind": "datetime", "value": "2032-05-01T12:00:00Z"}
    assert ref["deadline"] is None
    assert len(await task_repository.list(None)) == 1


@pytest.mark.asyncio
async def test_plan_tools_register_and_apply_through_real_handlers(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    registry = ToolRegistry()
    register_assistant_tools(registry, service)
    save_handler = registry.get("builtin.assistant.plans.save")
    apply_handler = registry.get("builtin.assistant.plans.apply")
    assert not save_handler.definition().needs_approval
    assert not apply_handler.definition().needs_approval

    saved = await save_handler.execute(
        {"summary": "拆分项目", "stage": "proposed", "items": [
            {"title": "准备", "planning": {"kind": "task", "scheduled_time": None}},
            {"title": "实现", "planning": {"kind": "task", "scheduled_time": None}},
        ]},
        make_context(call_id="plan-save", key="plan-save"),
    )
    request = json.loads(saved.output)["data"]["request"]
    applied = await apply_handler.execute(
        {"request_id": request["id"], "expected_version": request["version"]},
        make_context(call_id="plan-apply", key="plan-apply"),
    )
    assert not applied.is_error
    applied_refs = json.loads(applied.output)["task_refs"]
    assert len(applied_refs) == 2
    assert all(ref["planning"]["scheduled_time"] is None for ref in applied_refs)
    assert all(ref["planning"]["related_task_id"] is None for ref in applied_refs)
    assert len(await task_repository.list(None)) == 2
    rejected = await apply_handler.execute(
        {"request_id": request["id"], "expected_version": request["version"]},
        make_context(session_id="ordinary-session", call_id="wrong-identity", key="wrong-identity"),
    )
    assert rejected.is_error
    assert json.loads(rejected.output)["error"]["code"] == "configuration_error"


@pytest.mark.asyncio
async def test_before_event_reminder_rule_reaches_reminder_service(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    event = await service.create_task(
        TaskCreateInput(title="答辩", planning={
            "kind": "event", "event_time": {"kind": "datetime", "value": "2032-04-05T14:00:00Z"}
        }),
        make_context(),
    )
    result = await service.create_reminder(
        ReminderCreateInput(
            task_id=event.task_refs[0].id, expected_task_version=event.task_refs[0].version,
            rule={"kind": "before_event", "offset_minutes": 30},
        ),
        make_context(call_id="before-event", key="before-event"),
    )
    assert result.outcome == "succeeded"
    _, reminders = await task_repository.get_detail(event.task_refs[0].id)
    assert reminders[0].rule == {"kind": "before_event", "offset_minutes": 30}
