from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from dotclaw.assistant.schemas import (
    ReminderStatusInput,
    RequestSaveInput,
    ResultSetGetInput,
    TaskCreateInput,
    TaskPatch,
    TaskQueryInput,
    TaskStatusInput,
    TaskUpdateInput,
)
from dotclaw.assistant.service import (
    ASSISTANT_AGENT_ID,
    ASSISTANT_SESSION_ID,
    AssistantService,
)
from dotclaw.assistant.sqlite_repository import AssistantStorageError, SqliteAssistantRepository
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind
from dotclaw.tools.base import ToolExecutionContext


NOW = datetime(2032, 4, 5, 12, tzinfo=UTC)


class RunRepository:
    def __init__(self):
        self.runs = {}
        self.messages = {}
        self.ledgers = {}

    async def load_run_snapshot(self, session_id, run_id):
        return self.runs.get(run_id)

    async def load_messages(self, session_id, run_id):
        return self.messages.get(run_id, ())

    async def load_tool_ledger(self, session_id, run_id):
        return self.ledgers.get(run_id)


def make_context(*, run_id="run-1", call_id="call-1", key="key-1", message_id="msg-1", session_id=ASSISTANT_SESSION_ID):
    return ToolExecutionContext(
        agentrun_id=run_id,
        tool_call_id=call_id,
        idempotency_key=key,
        agent_id=ASSISTANT_AGENT_ID,
        session_id=session_id,
        input_message_id=message_id,
        input_created_at=NOW.isoformat(),
    )


def add_user_run(repository, *, run_id="run-1", message_id="msg-1", session_id=ASSISTANT_SESSION_ID):
    repository.runs[run_id] = SimpleNamespace(
        run_id=run_id,
        session_id=session_id,
        agent_id=ASSISTANT_AGENT_ID,
        input_message_id=message_id,
        state=SimpleNamespace(is_ended=lambda: False),
    )
    repository.messages[run_id] = (
        RunMessage(
            message_id=message_id,
            sequence=1,
            kind=RunMessageKind.USER_INPUT,
            role=MessageRole.USER,
            content="请记录一个事项",
            metadata={"created_at": NOW.isoformat()},
        ),
    )


async def make_service(path: Path):
    task_repository = SqlitePersonalTaskRepository(path, ZoneInfo("UTC"))
    await task_repository.initialize()
    run_repository = RunRepository()
    add_user_run(run_repository)
    service = AssistantService(
        path,
        timezone=ZoneInfo("UTC"),
        repository=SqliteAssistantRepository(path),
        clock=lambda: NOW,
    )
    service.bind_run_repository(run_repository)
    return service, task_repository, run_repository


@pytest.mark.asyncio
async def test_real_sqlite_create_patch_clear_and_query_snapshot(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    context = make_context()

    created = await service.create_task(
        TaskCreateInput(title="核对合同", description="初稿", deadline=date(2032, 4, 10)), context
    )
    assert created.outcome == "succeeded"
    task_ref = created.task_refs[0]
    assert task_ref.title == "核对合同"
    assert task_ref.description == "初稿"
    assert task_ref.deadline.kind == "date"
    assert task_ref.deadline.value == date(2032, 4, 10)
    assert task_ref.planning.kind == "task"

    updated = await service.update_task(
        TaskUpdateInput(
            task_id=task_ref.id,
            expected_version=task_ref.version,
            patch=TaskPatch(description=""),
        ),
        make_context(call_id="call-2", key="key-2"),
    )
    assert updated.outcome == "succeeded"
    detail, _ = await task_repository.get_detail(task_ref.id)
    assert detail.title == "核对合同"
    assert detail.description == ""
    assert detail.deadline.value == date(2032, 4, 10)

    cleared = await service.update_task(
        TaskUpdateInput(
            task_id=task_ref.id,
            expected_version=updated.task_refs[0].version,
            patch=TaskPatch(deadline=None),
        ),
        make_context(call_id="call-3", key="key-3"),
    )
    assert cleared.outcome == "succeeded"
    assert (await task_repository.get_detail(task_ref.id))[0].deadline is None

    page = await service.query_tasks(TaskQueryInput(keyword="合同"), make_context(call_id="call-4", key="key-4"))
    assert page.data.total == 1
    assert page.data.items[0].id == task_ref.id
    assert page.data.items[0].description == ""
    assert page.data.items[0].deadline is None
    assert page.data.items[0].planning.kind == "task"
    snapshot = await service.get_result_set(
        ResultSetGetInput(result_set_id=page.result_set_id),
        make_context(call_id="call-5", key="key-5"),
    )
    assert snapshot.data.result_set.items[0].id == task_ref.id
    assert snapshot.data.result_set.items[0].description == ""
    assert snapshot.data.result_set.items[0].planning.kind == "task"


@pytest.mark.asyncio
async def test_task_query_passes_plan_filter_and_ended_uses_all_status_filter(tmp_path):
    service, _, _ = await make_service(tmp_path / "assistant.sqlite3")
    created = await service.create_task(TaskCreateInput(title="已结束事务"), make_context())
    await service.change_task_status(
        TaskStatusInput(task_id=created.task_refs[0].id, expected_version=1, status="completed"),
        make_context(call_id="complete", key="complete"),
    )
    ended_active = await service.query_tasks(TaskQueryInput(plan_filter="ended"), make_context(call_id="active-ended", key="active-ended"))
    ended_all = await service.query_tasks(TaskQueryInput(filter="all", plan_filter="ended"), make_context(call_id="all-ended", key="all-ended"))
    assert ended_active.data.total == 0
    assert ended_all.data.total == 1
    assert ended_all.data.items[0].id == created.task_refs[0].id


@pytest.mark.asyncio
async def test_planning_receipts_render_local_offset_without_turning_date_into_midnight(tmp_path):
    path = tmp_path / "assistant.sqlite3"
    timezone = ZoneInfo("Asia/Shanghai")
    task_repository = SqlitePersonalTaskRepository(path, timezone)
    await task_repository.initialize()
    service = AssistantService(
        path, timezone=timezone, repository=SqliteAssistantRepository(path), clock=lambda: NOW,
    )
    date_event = await service.create_task(
        TaskCreateInput(title="日期事件", planning={
            "kind": "event", "event_time": {"kind": "date", "value": "2032-04-10"},
        }),
        make_context(call_id="date-event", key="date-event"),
    )
    timed_event = await service.create_task(
        TaskCreateInput(title="精确事件", planning={
            "kind": "event", "event_time": {"kind": "datetime", "value": "2032-04-11T20:00:00+08:00"},
        }),
        make_context(call_id="timed-event", key="timed-event"),
    )

    assert date_event.task_refs[0].planning.event_time.value == date(2032, 4, 10)
    local_time = timed_event.task_refs[0].planning.event_time.value
    assert isinstance(local_time, datetime)
    assert local_time.isoformat() == "2032-04-11T20:00:00+08:00"
    stored, _ = await task_repository.get_detail(timed_event.task_refs[0].id)
    assert stored.planning.event_time.value.astimezone(UTC).isoformat() == "2032-04-11T12:00:00+00:00"

    page = await service.query_tasks(TaskQueryInput(filter="all"), make_context(call_id="events-page", key="events-page"))
    snapshot = await service.get_result_set(
        ResultSetGetInput(result_set_id=page.result_set_id),
        make_context(call_id="events-snapshot", key="events-snapshot"),
    )
    page_event = next(item for item in page.data.items if item.id == timed_event.task_refs[0].id)
    result_event = next(item for item in snapshot.data.result_set.items if item.id == timed_event.task_refs[0].id)
    assert page_event.planning.event_time.value.isoformat() == "2032-04-11T20:00:00+08:00"
    assert result_event.planning.event_time.value.isoformat() == "2032-04-11T20:00:00+08:00"

    cleared = await service.update_task(
        TaskUpdateInput(
            task_id=timed_event.task_refs[0].id, expected_version=timed_event.task_refs[0].version,
            patch={"planning": {"kind": "event", "event_time": None, "time_text": "晚上"}},
        ),
        make_context(call_id="clear-event-time", key="clear-event-time"),
    )
    assert cleared.task_refs[0].planning.event_time is None
    assert cleared.task_refs[0].planning.time_text == "晚上"
    cancelled = await service.change_task_status(
        TaskStatusInput(task_id=timed_event.task_refs[0].id, expected_version=cleared.task_refs[0].version, status="cancelled"),
        make_context(call_id="cancel-event", key="cancel-event"),
    )
    persisted, _ = await task_repository.get_detail(timed_event.task_refs[0].id)
    assert cancelled.task_refs[0].status == "cancelled"
    assert persisted.planning.event_time is None
    assert persisted.planning.time_text == "晚上"


@pytest.mark.asyncio
async def test_reminder_status_mutates_persisted_rule_and_task_link(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    create = await service.create_task(
        TaskCreateInput(
            title="提交申请",
            reminders=[{"kind": "daily", "local_time": "13:00"}],
        ),
        make_context(),
    )
    task = create.task_refs[0]
    reminder = create.reminder_refs[0]
    assert reminder.status == "active"

    paused = await service.change_reminder_status(
        ReminderStatusInput(
            task_id=task.id,
            reminder_id=reminder.id,
            expected_task_version=task.version,
            expected_version=reminder.version,
            action="pause",
        ),
        make_context(call_id="call-2", key="key-2"),
    )
    assert paused.outcome == "succeeded"
    saved_task, saved_reminders = await task_repository.get_detail(task.id)
    assert saved_task.version == task.version
    assert saved_reminders[0].status.value == "paused"
    assert saved_reminders[0].next_fire_at is None


@pytest.mark.asyncio
async def test_idempotent_replay_conflict_and_failed_draft_has_no_task_side_effect(tmp_path):
    service, task_repository, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    create_input = TaskCreateInput(title="只创建一次")
    context = make_context()
    first = await service.create_task(create_input, context)
    replay = await service.create_task(create_input, context)
    conflict = await service.create_task(TaskCreateInput(title="不同参数"), context)
    assert replay.model_dump(mode="json") == first.model_dump(mode="json")
    assert conflict.error.code == "key_conflict"
    assert len(await task_repository.list(None)) == 1

    payload = {
        "intent_args": {"action": "create_task", "title": None},
        "missing_fields": ["title"],
        "candidate_task_ids": [],
        "selected_task_ids": [],
        "temporal_values": [],
        "last_error": None,
    }
    add_user_run(run_repository, run_id="run-2", message_id="msg-2")
    draft = await service.save_request(
        RequestSaveInput(kind="create_task", summary="待补充标题", payload=payload, source_refs=[]),
        make_context(run_id="run-2", call_id="call-1", key="key-3", message_id="msg-2"),
    )
    assert draft.outcome == "succeeded"
    assert draft.data.request.status == "awaiting_input"
    assert len(await task_repository.list(None)) == 1

    add_user_run(run_repository, run_id="other-run", message_id="other-message", session_id="other-session")
    add_user_run(run_repository, run_id="run-3", message_id="msg-3")
    bad_payload = {**payload, "temporal_values": [{
        "field": "deadline", "original_text": "明天", "source_ref": {"run_id": "other-run", "message_id": "other-message"},
        "resolved_value": "2032-04-06", "timezone": "UTC",
    }]}
    bad_source = await service.save_request(
        RequestSaveInput(kind="create_task", summary="跨会话来源", payload=bad_payload, source_refs=[{"run_id": "other-run", "message_id": "other-message"}]),
        make_context(run_id="run-3", call_id="call-1", key="key-4", message_id="msg-3"),
    )
    assert bad_source.outcome == "failed"
    assert bad_source.error.code == "invalid_arguments"
    assert len(await task_repository.list(None)) == 1


@pytest.mark.asyncio
async def test_completing_task_cancels_its_persisted_reminders(tmp_path):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")
    created = await service.create_task(
        TaskCreateInput(title="完成后停止提醒", reminders=[{"kind": "daily", "local_time": "13:00"}]),
        make_context(),
    )
    task = created.task_refs[0]
    reminder = created.reminder_refs[0]
    changed = await service.change_task_status(
        TaskStatusInput(task_id=task.id, expected_version=task.version, status="completed"),
        make_context(call_id="complete", key="complete"),
    )
    assert changed.outcome == "succeeded"
    persisted_task, reminders = await task_repository.get_detail(task.id)
    assert persisted_task.status.value == "completed"
    assert reminders[0].status.value == "cancelled"
    assert changed.reminder_refs[0].id == reminder.id

@pytest.mark.asyncio
async def test_receipt_storage_failure_rolls_back_the_real_task_write(tmp_path, monkeypatch):
    service, task_repository, _ = await make_service(tmp_path / "assistant.sqlite3")

    async def fail_receipt(*args, **kwargs):
        raise AssistantStorageError("测试注入回执写失败")

    monkeypatch.setattr(service._repository, "insert_operation", fail_receipt)
    with pytest.raises(AssistantStorageError, match="测试注入回执写失败"):
        await service.create_task(TaskCreateInput(title="不得部分提交"), make_context())

    assert await task_repository.list(None) == ()


def test_strict_inputs_reject_extra_fields_and_empty_patch():
    with pytest.raises(ValidationError):
        TaskQueryInput.model_validate({"unexpected": True})
    with pytest.raises(ValidationError):
        TaskUpdateInput(task_id="t", expected_version=1, patch={})
