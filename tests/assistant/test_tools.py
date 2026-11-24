import json

import pytest

from dotclaw.tools.assistant_tools import register_assistant_tools
from dotclaw.tools.registry import ToolRegistry
from dotclaw.assistant.schemas import TaskCreateInput
from dotclaw.assistant.service import ASSISTANT_TOOL_OPERATIONS

from .test_service import add_user_run, make_context, make_service


@pytest.mark.asyncio
async def test_registered_tool_executes_real_service_and_rejects_wrong_identity(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    registry = ToolRegistry()
    register_assistant_tools(registry, service)
    assert set(registry.all_names()) == set(ASSISTANT_TOOL_OPERATIONS)

    handler = registry.get("builtin.assistant.tasks.create")
    result = await handler.execute({"title": "通过注册工具创建"}, make_context())
    assert not result.is_error
    assert json.loads(result.output)["task_refs"][0]["title"] == "通过注册工具创建"
    assert len(await task_repository.list(None)) == 1

    wrong_context = make_context(session_id="ordinary-session", call_id="bad", key="bad-key")
    rejected = await handler.execute({"title": "不应创建"}, wrong_context)
    assert rejected.is_error
    assert json.loads(rejected.output)["error"]["code"] == "configuration_error"
    assert len(await task_repository.list(None)) == 1


@pytest.mark.asyncio
async def test_batch_tool_contract_applies_without_runtime_approval(tmp_path):
    """批量提案的版本校验不依赖运行时审批。"""
    service, task_repository, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    first = await service.create_task(TaskCreateInput(title="待批一"), make_context(call_id="create-1", key="create-1"))
    second = await service.create_task(TaskCreateInput(title="待批二"), make_context(call_id="create-2", key="create-2"))
    first_task = first.task_refs[0]
    second_task = second.task_refs[0]
    add_user_run(run_repository, run_id="batch-run", message_id="batch-message")
    registry = ToolRegistry()
    register_assistant_tools(registry, service)
    prepare_handler = registry.get("builtin.assistant.batch.prepare")
    apply_handler = registry.get("builtin.assistant.batch.apply")
    prepare_definition = prepare_handler.definition()
    apply_definition = apply_handler.definition()
    query_definition = registry.get("builtin.assistant.tasks.query").definition()

    assert not prepare_definition.needs_approval
    assert not apply_definition.needs_approval
    assert "batch.apply" in prepare_definition.description
    assert "request_id 与 expected_request_version 必须同时提供" in prepare_definition.description
    assert "requests.get 返回的 data.request.version" in prepare_definition.description
    assert "执行" in apply_definition.description
    assert "filter=all" in query_definition.description
    assert "由运行时暂停并请求结构化批准" not in prepare_definition.description
    assert "data.request.id" in registry.get("builtin.assistant.plans.save").definition().description
    assert "data.request.version" in registry.get("builtin.assistant.plans.apply").definition().description
    assert "filter=all" in query_definition.description

    prepared = await prepare_handler.execute(
        {
            "summary": "更新两项",
            "items": [
                {"action": "update", "task_id": first_task.id, "expected_version": first_task.version,
                 "patch": {"title": "已批一"}},
                {"action": "update", "task_id": second_task.id, "expected_version": second_task.version,
                 "patch": {"title": "已批二"}},
            ],
        },
        make_context(run_id="batch-run", call_id="prepare", key="prepare", message_id="batch-message"),
    )
    assert not prepared.is_error
    prepared_request = json.loads(prepared.output)["data"]["request"]
    assert prepared_request["status"] == "awaiting_confirmation"

    apply_result = await apply_handler.execute(
        {
            "request_id": prepared_request["id"],
            "expected_version": prepared_request["version"],
            "proposal_hash": prepared_request["proposal_hash"],
        },
        make_context(run_id="batch-run", call_id="apply", key="apply", message_id="batch-message"),
    )

    assert not apply_result.is_error
    assert (await task_repository.get_detail(first_task.id))[0].title == "已批一"
    assert (await task_repository.get_detail(second_task.id))[0].title == "已批二"


@pytest.mark.asyncio
async def test_requests_save_describes_required_payload_and_saves_unresolved_reminder(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    registry = ToolRegistry()
    register_assistant_tools(registry, service)
    handler = registry.get("builtin.assistant.requests.save")

    description = handler.definition().description
    assert "payload.temporal_values传[]" in description
    assert "source_refs可传[]" in description
    assert "不要自行填写运行或消息ID" in description

    payload = {
        "intent_args": {"action": "reminder", "task_id": None, "reminder_id": None, "rule": None},
        "missing_fields": ["提醒时间"],
        "candidate_task_ids": [],
        "selected_task_ids": [],
        "temporal_values": [],
        "last_error": None,
    }
    missing_values = await handler.execute(
        {
            "kind": "reminder",
            "summary": "为【B5验收】提交报名材料设置提醒，待确认提醒时间",
            "payload": {key: value for key, value in payload.items() if key != "temporal_values"},
            "source_refs": [],
        },
        make_context(call_id="missing-temporal-values", key="missing-temporal-values"),
    )
    assert missing_values.is_error
    missing_error = json.loads(missing_values.output)["error"]
    assert missing_error["code"] == "invalid_arguments"
    assert "payload.temporal_values" in missing_error["message"]

    extra_top_level_value = await handler.execute(
        {
            "kind": "reminder",
            "summary": "为【B5验收】提交报名材料设置提醒，待确认提醒时间",
            "payload": payload,
            "source_refs": [],
            "temporal_values": [],
        },
        make_context(call_id="extra-top-level-temporal-values", key="extra-top-level-temporal-values"),
    )
    assert extra_top_level_value.is_error
    assert json.loads(extra_top_level_value.output)["error"]["code"] == "invalid_arguments"

    saved = await handler.execute(
        {
            "kind": "reminder",
            "summary": "为【B5验收】提交报名材料设置提醒，待确认提醒时间",
            "payload": payload,
            "source_refs": [],
        },
        make_context(call_id="save-unresolved-reminder", key="save-unresolved-reminder"),
    )
    assert not saved.is_error
    request = json.loads(saved.output)["data"]["request"]
    assert request["status"] == "awaiting_input"
    assert request["payload"]["missing_fields"] == ["提醒时间"]
    assert request["payload"]["temporal_values"] == []
    assert len(request["source_refs"]) == 1
    assert not await task_repository.list(None)


@pytest.mark.asyncio
async def test_task_create_request_reference_requires_version_before_side_effects(tmp_path):
    """缺关联版本时不创建事务；配对版本后创建提醒并解析请求。"""
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    registry = ToolRegistry()
    register_assistant_tools(registry, service)
    save_handler = registry.get("builtin.assistant.requests.save")
    get_handler = registry.get("builtin.assistant.requests.get")
    create_handler = registry.get("builtin.assistant.tasks.create")

    draft = await save_handler.execute(
        {
            "kind": "create_task",
            "summary": "创建带提醒的事务",
            "payload": {
                "intent_args": {
                    "action": "create_task",
                    "title": None,
                    "description": None,
                    "deadline": None,
                    "reminders": [],
                },
                "missing_fields": ["事务标题"],
                "candidate_task_ids": [],
                "selected_task_ids": [],
                "temporal_values": [],
                "last_error": None,
            },
            "source_refs": [],
        },
        make_context(call_id="save-create-draft", key="save-create-draft"),
    )
    assert not draft.is_error
    request = json.loads(draft.output)["data"]["request"]

    missing_version = await create_handler.execute(
        {"title": "确定后的事务", "request_id": request["id"]},
        make_context(call_id="create-without-version", key="create-without-version"),
    )
    assert missing_version.is_error
    error = json.loads(missing_version.output)["error"]
    assert error["code"] == "invalid_arguments"
    assert "request_id" in error["message"]
    assert "expected_request_version" in error["message"]
    assert "data.request.version" in error["message"]
    assert request["id"] not in error["message"]
    assert not await task_repository.list(None)
    still_waiting = await get_handler.execute(
        {"id": request["id"]}, make_context(call_id="check-draft", key="check-draft")
    )
    assert json.loads(still_waiting.output)["data"]["request"]["status"] == "awaiting_input"

    created = await create_handler.execute(
        {
            "title": "确定后的事务",
            "request_id": request["id"],
            "expected_request_version": request["version"],
            "reminders": [{"kind": "at", "at": "2032-04-10T09:00:00+00:00"}],
        },
        make_context(call_id="create-with-version", key="create-with-version"),
    )
    assert not created.is_error
    operation = json.loads(created.output)
    assert operation["request_id"] == request["id"]
    assert len(operation["task_refs"]) == 1
    assert len(operation["reminder_refs"]) == 1
    resolved = await get_handler.execute(
        {"id": request["id"]}, make_context(call_id="check-resolved", key="check-resolved")
    )
    assert json.loads(resolved.output)["data"]["request"]["status"] == "resolved"
    assert len(await task_repository.list(None)) == 1
