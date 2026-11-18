from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskStatus, TaskDeadline
from dotclaw.personal_tasks.repository import PersonalTaskNotFound, PersonalTaskVersionConflict
from dotclaw.personal_tasks.service import InvalidPersonalTaskTransition, PersonalTaskService


class MemoryRepository:
    def __init__(self):
        self.items = {}

    async def create(self, task):
        self.items[task.id] = task
        return task

    async def get(self, task_id):
        return self.items.get(task_id)

    async def list(self, statuses):
        return tuple(task for task in self.items.values() if statuses is None or task.status in statuses)

    async def replace(self, task, *, expected_version):
        current = self.items.get(task.id)
        if current is None:
            raise PersonalTaskNotFound(task.id)
        if current.version != expected_version:
            raise PersonalTaskVersionConflict(task.id)
        self.items[task.id] = task
        return task

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
