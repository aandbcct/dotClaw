import asyncio
import sqlite3
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskStatus
from dotclaw.personal_tasks.repository import PersonalTaskStorageError, PersonalTaskVersionConflict
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from dotclaw.personal_tasks.reminder_models import Reminder, ReminderStatus


def task(version=1):
    now = datetime(2026, 11, 18, tzinfo=UTC)
    return PersonalTask("t1", "事务", "", PersonalTaskStatus.PENDING, None, now, now, None, version)


@pytest.mark.asyncio
async def test_reopen_persists_and_compare_and_swap_allows_one_writer(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    first = SqlitePersonalTaskRepository(path)
    second = SqlitePersonalTaskRepository(path)
    await first.initialize()
    await first.create(task())
    reopened = SqlitePersonalTaskRepository(path)
    await reopened.initialize()
    assert await reopened.get("t1") == task()
    left = task(2)
    right = PersonalTask("t1", "另一个标题", "", PersonalTaskStatus.PENDING, None,
        left.created_at, left.updated_at, None, 2)
    outcomes = await asyncio.gather(first.replace(left, expected_version=1), second.replace(right, expected_version=1), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in outcomes) == 1
    assert sum(isinstance(result, PersonalTaskVersionConflict) for result in outcomes) == 1
    assert (await reopened.get("t1")).version == 2


def test_connect_maps_pragma_failure_and_closes_connection(tmp_path, monkeypatch):
    class Connection:
        closed = False
        row_factory = None

        def execute(self, statement):
            raise sqlite3.OperationalError("readonly")

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)
    repository = SqlitePersonalTaskRepository(tmp_path / "tasks.sqlite3")
    with pytest.raises(PersonalTaskStorageError) as captured:
        repository._connect()
    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
    assert connection.closed


@pytest.mark.asyncio
async def test_create_task_and_reminders_roll_back_as_one_transaction(tmp_path):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    repository = SqlitePersonalTaskRepository(tmp_path / "atomic-create.sqlite3")
    await repository.initialize()
    new_task = PersonalTask("task-1", "事务", "", PersonalTaskStatus.PENDING, None, now, now, None, 1)
    reminder = Reminder("reminder-1", new_task.id, {"kind": "daily", "local_time": "09:00"}, "UTC",
                        ReminderStatus.ACTIVE, None, now.replace(hour=9), 1, now, now)

    with pytest.raises(PersonalTaskStorageError):
        await repository.create_with_reminders(new_task, (reminder, reminder))

    assert await repository.get(new_task.id) is None


@pytest.mark.asyncio
async def test_query_snapshot_summaries_and_detail(tmp_path):
    from dotclaw.personal_tasks.models import PersonalTaskQuery, TaskDeadline

    now = datetime(2030, 1, 1, tzinfo=UTC)
    repository = SqlitePersonalTaskRepository(tmp_path / "query.sqlite3")
    await repository.initialize()
    first = PersonalTask("first", "含 % 字符", "说明 _ 字符", PersonalTaskStatus.PENDING,
        TaskDeadline("date", date(2030, 1, 2)), now, now, None, 1)
    second = PersonalTask("second", "另一个", "", PersonalTaskStatus.PENDING,
        TaskDeadline("date", date(2030, 1, 3)), now, now, None, 1)
    await repository.create(first)
    await repository.create(second)
    reminder = Reminder("r1", first.id, {"kind": "daily", "local_time": "09:00"}, "UTC",
        ReminderStatus.ACTIVE, None, now.replace(hour=9), 1, now, now)
    await repository.create_reminder(reminder, expected_task_version=1)

    tasks, summaries = await repository.query(PersonalTaskQuery())
    detail_task, reminders = await repository.get_detail(first.id)
    assert {item.id for item in tasks} == {first.id, second.id}
    assert summaries[first.id].active_count == 1
    assert summaries[first.id].next_fire_at == now.replace(hour=9)
    assert summaries[second.id].active_count == 0
    assert detail_task == first
    assert reminders == (reminder,)


@pytest.mark.asyncio
async def test_replace_with_effects_reports_changes_inside_update(tmp_path):
    from dotclaw.personal_tasks.models import TaskDeadline

    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    repository = SqlitePersonalTaskRepository(tmp_path / "effects.sqlite3", ZoneInfo("UTC"))
    await repository.initialize()
    task_value = PersonalTask("effects", "事务", "", PersonalTaskStatus.PENDING,
        TaskDeadline("datetime", datetime(2030, 1, 5, 12, tzinfo=UTC)), now, now, None, 1)
    await repository.create(task_value)
    reminder = Reminder("relative", task_value.id, {"kind": "before_deadline", "offset_minutes": 60}, "UTC",
        ReminderStatus.ACTIVE, None, datetime(2030, 1, 5, 11, tzinfo=UTC), 1, now, now)
    await repository.create_reminder(reminder, expected_task_version=1)
    from dotclaw.personal_tasks.service import PersonalTaskService

    service = PersonalTaskService(repository, timezone=ZoneInfo("UTC"), clock=lambda: now)
    updated = await service.update(task_value.id, title="事务", description="", deadline=
        TaskDeadline("datetime", datetime(2030, 1, 6, 12, tzinfo=UTC)), expected_version=1)
    assert [(item.action, item.version) for item in updated.reminder_changes] == [("rescheduled", 2)]
    completed = await service.change_status(task_value.id, status=PersonalTaskStatus.COMPLETED, expected_version=2)
    assert [(item.action, item.reason, item.version) for item in completed.reminder_changes] == [("cancelled", "task_ended", 3)]
