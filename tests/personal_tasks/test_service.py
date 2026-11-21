from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskFilter, PersonalTaskStatus, TaskDeadline
from dotclaw.personal_tasks.repository import PersonalTaskNotFound, PersonalTaskVersionConflict
from dotclaw.personal_tasks.service import InvalidPersonalTaskTransition, PersonalTaskService


class MemoryRepository:
    def __init__(self):
        self.items = {}
        self.reminders = {}

    async def create(self, task):
        self.items[task.id] = task
        return task

    async def create_with_reminders(self, task, reminders):
        self.items[task.id] = task
        self.reminders[task.id] = reminders
        return task

    async def get(self, task_id):
        return self.items.get(task_id)

    async def list(self, statuses):
        return tuple(task for task in self.items.values() if statuses is None or task.status in statuses)

    async def query(self, query):
        from dotclaw.personal_tasks.models import ReminderSummary

        return tuple(self.items.values()), {task.id: ReminderSummary(0, None) for task in self.items.values()}

    async def replace(self, task, *, expected_version, now=None, timezone=None):
        current = self.items.get(task.id)
        if current is None:
            raise PersonalTaskNotFound(task.id)
        if current.version != expected_version:
            raise PersonalTaskVersionConflict(task.id)
        self.items[task.id] = task
        return task

    async def replace_with_effects(self, task, *, expected_version, now=None, timezone=None):
        return await self.replace(task, expected_version=expected_version, now=now, timezone=timezone), ()

    async def delete(self, task_id, *, expected_version):
        current = self.items.get(task_id)
        if current is None:
            raise PersonalTaskNotFound(task_id)
        if current.version != expected_version:
            raise PersonalTaskVersionConflict(task_id)
        del self.items[task_id]


@pytest.mark.asyncio
async def test_status_completion_edit_and_reopen_rules():
    now = datetime(2026, 11, 18, 4, tzinfo=UTC)
    repository = MemoryRepository()
    service = PersonalTaskService(repository, timezone=ZoneInfo("Asia/Shanghai"), clock=lambda: now)
    first = await service.create(title=" same ", deadline=TaskDeadline("date", date(2026, 11, 18)))
    second = await service.create(title="same")
    assert first.task.title == second.task.title
    completed = await service.change_status(first.task.id, status=PersonalTaskStatus.COMPLETED, expected_version=1)
    edited = await service.update(first.task.id, title="same", description="", deadline=first.task.deadline, expected_version=2)
    assert edited.task.completed_at == completed.task.completed_at
    reopened = await service.change_status(first.task.id, status=PersonalTaskStatus.PENDING, expected_version=3)
    assert reopened.task.completed_at is None
    with pytest.raises(InvalidPersonalTaskTransition):
        await service.change_status(first.task.id, status=PersonalTaskStatus.PENDING, expected_version=4)


@pytest.mark.asyncio
async def test_date_overdue_boundary_uses_app_timezone_and_terminal_status():
    now = datetime(2026, 11, 18, 16, 0, tzinfo=UTC)
    service = PersonalTaskService(MemoryRepository(), timezone=ZoneInfo("Asia/Shanghai"), clock=lambda: now)
    today = await service.create(title="today", deadline=TaskDeadline("date", date(2026, 11, 19)))
    yesterday = await service.create(title="yesterday", deadline=TaskDeadline("date", date(2026, 11, 18)))
    assert not today.is_overdue
    assert yesterday.is_overdue
    cancelled = await service.change_status(yesterday.task.id, status=PersonalTaskStatus.CANCELLED, expected_version=1)
    assert not cancelled.is_overdue


@pytest.mark.asyncio
async def test_datetime_overdue_is_strictly_after_deadline():
    deadline = datetime(2026, 11, 18, 16, 0, tzinfo=UTC)
    repository = MemoryRepository()
    exact = PersonalTaskService(repository, timezone=ZoneInfo("Asia/Shanghai"), clock=lambda: deadline)
    task_at_boundary = await exact.create(title="at", deadline=TaskDeadline("datetime", deadline))
    assert not task_at_boundary.is_overdue
    after = PersonalTaskService(repository, timezone=ZoneInfo("Asia/Shanghai"), clock=lambda: deadline.replace(microsecond=1))
    task_after = await after.get(task_at_boundary.task.id)
    assert task_after.is_overdue


@pytest.mark.asyncio
async def test_create_initial_reminders_validates_all_before_persisting():
    from dotclaw.personal_tasks.repository import InvalidReminderInput

    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    repository = MemoryRepository()
    service = PersonalTaskService(repository, timezone=ZoneInfo("UTC"), clock=lambda: now)
    future = now.replace(minute=1).isoformat().replace("+00:00", "Z")
    with pytest.raises(InvalidReminderInput):
        await service.create(title="事务", reminders=(
            {"kind": "at", "at": future},
            {"kind": "before_deadline", "offset_minutes": 60},
        ))
    assert repository.items == {}

    created = await service.create(title="事务", reminders=({"kind": "at", "at": future},))
    assert len(repository.reminders[created.task.id]) == 1


@pytest.mark.asyncio
async def test_query_uses_literal_keyword_local_dates_and_stable_pagination():
    from dotclaw.personal_tasks.models import PersonalTaskQuery

    now = datetime(2026, 11, 20, 16, tzinfo=UTC)
    service = PersonalTaskService(MemoryRepository(), timezone=ZoneInfo("Asia/Shanghai"), clock=lambda: now)
    overdue = await service.create(title="100%_匹配", description="文字", deadline=TaskDeadline("date", date(2026, 11, 20)))
    today = await service.create(title="literal %_", deadline=TaskDeadline("date", date(2026, 11, 21)))
    await service.create(title="无截止事务")

    result = await service.query(PersonalTaskQuery(keyword=" %_ ", deadline_from=date(2026, 11, 21),
        deadline_until=date(2026, 11, 22), offset=0, limit=10))
    assert [item.task.id for item in result.items] == [today.task.id]
    overdue_only = await service.query(PersonalTaskQuery(keyword="%_", overdue_only=True))
    assert [item.task.id for item in overdue_only.items] == [overdue.task.id]
    page = await service.query(PersonalTaskQuery(filter=PersonalTaskFilter.ALL, offset=1, limit=1))
    assert page.total == 3
    assert len(page.items) == 1
    assert page.offset == 1 and page.limit == 1
    assert page.items[0].task.id == today.task.id
