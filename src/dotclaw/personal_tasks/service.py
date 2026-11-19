"""个人事务的确定性业务服务。"""

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import (PersonalTask, PersonalTaskFilter, PersonalTaskList, PersonalTaskStatus,
                     PersonalTaskView, TaskDeadline, deadline_sort_at, is_overdue)
from .repository import PersonalTaskNotFound, PersonalTaskRepository, PersonalTaskVersionConflict


class InvalidPersonalTaskInput(ValueError):
    pass


class InvalidPersonalTaskTransition(Exception):
    def __init__(self, task_id: str, current: PersonalTaskStatus, target: PersonalTaskStatus):
        self.task_id, self.current, self.target = task_id, current, target
        super().__init__(f"不允许事务状态从 {current} 转为 {target}")


class PersonalTaskService:
    def __init__(self, repository: PersonalTaskRepository, *, timezone: ZoneInfo,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._repository = repository
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))

    def _view(self, task: PersonalTask, now: datetime) -> PersonalTaskView:
        return PersonalTaskView(task, is_overdue(task, now, self._timezone))

    async def create(self, *, title: str, description: str = "", deadline: TaskDeadline | None = None) -> PersonalTaskView:
        title = title.strip()
        if not title:
            raise InvalidPersonalTaskInput("标题不能为空")
        now = self._clock().astimezone(UTC)
        task = PersonalTask(str(uuid4()), title, description, PersonalTaskStatus.PENDING,
                            deadline, now, now, None, 1)
        return self._view(await self._repository.create(task), now)

    async def get(self, task_id: str) -> PersonalTaskView:
        task = await self._repository.get(task_id)
        if task is None:
            raise PersonalTaskNotFound(task_id)
        return self._view(task, self._clock().astimezone(UTC))

    async def list(self, filter: PersonalTaskFilter = PersonalTaskFilter.ACTIVE) -> PersonalTaskList:
        statuses = {
            PersonalTaskFilter.ACTIVE: (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS),
            PersonalTaskFilter.ALL: None,
            PersonalTaskFilter.COMPLETED: (PersonalTaskStatus.COMPLETED,),
            PersonalTaskFilter.CANCELLED: (PersonalTaskStatus.CANCELLED,),
        }[filter]
        tasks = await self._repository.list(statuses)
        now = self._clock().astimezone(UTC)
        tasks = tuple(sorted(tasks, key=lambda t: (t.deadline is None,
            deadline_sort_at(t.deadline, self._timezone) if t.deadline else t.created_at,
            t.created_at, t.id)))
        return PersonalTaskList(tuple(self._view(task, now) for task in tasks), str(self._timezone), now)

    async def update(self, task_id: str, *, title: str, description: str, deadline: TaskDeadline | None,
                     expected_version: int) -> PersonalTaskView:
        title = title.strip()
        if not title or expected_version < 1:
            raise InvalidPersonalTaskInput("标题不能为空且 expected_version 必须为正数")
        current = await self._repository.get(task_id)
        if current is None:
            raise PersonalTaskNotFound(task_id)
        if current.version != expected_version:
            raise PersonalTaskVersionConflict(task_id)
        now = self._clock().astimezone(UTC)
        updated = PersonalTask(current.id, title, description, current.status, deadline,
                               current.created_at, now, current.completed_at, current.version + 1)
        return self._view(await self._repository.replace(updated, expected_version=expected_version, now=now, timezone=self._timezone), now)

    async def change_status(self, task_id: str, *, status: PersonalTaskStatus, expected_version: int) -> PersonalTaskView:
        current = await self._repository.get(task_id)
        if current is None:
            raise PersonalTaskNotFound(task_id)
        if current.version != expected_version:
            raise PersonalTaskVersionConflict(task_id)
        allowed = {
            PersonalTaskStatus.PENDING: {PersonalTaskStatus.IN_PROGRESS, PersonalTaskStatus.COMPLETED, PersonalTaskStatus.CANCELLED},
            PersonalTaskStatus.IN_PROGRESS: {PersonalTaskStatus.PENDING, PersonalTaskStatus.COMPLETED, PersonalTaskStatus.CANCELLED},
            PersonalTaskStatus.COMPLETED: {PersonalTaskStatus.PENDING},
            PersonalTaskStatus.CANCELLED: {PersonalTaskStatus.PENDING},
        }
        if status not in allowed[current.status]:
            raise InvalidPersonalTaskTransition(task_id, current.status, status)
        now = self._clock().astimezone(UTC)
        updated = PersonalTask(current.id, current.title, current.description, status, current.deadline,
                               current.created_at, now, now if status == PersonalTaskStatus.COMPLETED else None,
                               current.version + 1)
        return self._view(await self._repository.replace(updated, expected_version=expected_version, now=now, timezone=self._timezone), now)

    async def delete(self, task_id: str, *, expected_version: int) -> None:
        await self._repository.delete(task_id, expected_version=expected_version)
