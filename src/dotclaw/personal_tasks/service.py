"""个人事务的确定性业务服务。"""

from datetime import UTC, date, datetime
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import (PersonalTask, PersonalTaskFilter, PersonalTaskList, PersonalTaskQuery, PersonalTaskStatus,
                     PersonalTaskView, TaskDeadline, TaskPlanning, PersonalTaskKind, PlanFilter, deadline_sort_at, is_overdue)
from .reminder_models import Reminder, ReminderRule, ReminderStatus
from .reminder_rules import next_fire_at, normalize_rule, validate_rule
from .repository import InvalidReminderInput, PersonalTaskNotFound, PersonalTaskRepository, PersonalTaskVersionConflict


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

    def _view(self, task: PersonalTask, now: datetime, reminder_changes=()) -> PersonalTaskView:
        return PersonalTaskView(task, is_overdue(task, now, self._timezone), tuple(reminder_changes))

    async def _validate_planning(self, planning: TaskPlanning, task_id: str | None = None) -> None:
        if not isinstance(planning, TaskPlanning) or not isinstance(planning.kind, PersonalTaskKind) or not isinstance(planning.time_text, str):
            raise InvalidPersonalTaskInput("planning 字段无效")
        if planning.kind == PersonalTaskKind.EVENT and planning.scheduled_time is not None:
            raise InvalidPersonalTaskInput("事件不能填写任务执行时间")
        if planning.kind == PersonalTaskKind.TASK and planning.event_time is not None:
            raise InvalidPersonalTaskInput("任务不能填写事件时间")
        if planning.event_time is not None and not isinstance(planning.event_time, TaskDeadline):
            raise InvalidPersonalTaskInput("event_time 必须是 TaskDeadline")
        if planning.scheduled_time is not None and not isinstance(planning.scheduled_time, TaskDeadline):
            raise InvalidPersonalTaskInput("scheduled_time 必须是 TaskDeadline")
        if planning.related_task_id is not None:
            related = await self._repository.get(planning.related_task_id)
            if related is None or related.planning.kind != PersonalTaskKind.EVENT or related.id == task_id:
                raise InvalidPersonalTaskInput("关联事务必须是已有事件")

    async def create(self, *, title: str, description: str = "", deadline: TaskDeadline | None = None,
                     reminders: tuple[ReminderRule, ...] = (), planning: TaskPlanning | None = None) -> PersonalTaskView:
        if not isinstance(title, str) or not isinstance(description, str) or not isinstance(reminders, tuple):
            raise InvalidPersonalTaskInput("事务字段类型无效")
        if deadline is not None and not isinstance(deadline, TaskDeadline):
            raise InvalidPersonalTaskInput("deadline 必须是 TaskDeadline 或 None")
        planning = planning or TaskPlanning()
        await self._validate_planning(planning)
        title = title.strip()
        if not title:
            raise InvalidPersonalTaskInput("标题不能为空")
        now = self._clock().astimezone(UTC)
        task = PersonalTask(str(uuid4()), title, description, PersonalTaskStatus.PENDING,
                            deadline, now, now, None, 1, planning)
        if not reminders:
            created = await self._repository.create(task)
        else:
            initial: list[Reminder] = []
            for rule in reminders:
                try:
                    validate_rule(rule, task, self._timezone)
                    candidate = next_fire_at(rule, task, self._timezone, now)
                except (ValueError, KeyError, TypeError) as error:
                    raise InvalidReminderInput(str(error)) from error
                if candidate is None and rule["kind"] != "before_event":
                    raise InvalidReminderInput("提醒时间必须晚于当前时间")
                rule = normalize_rule(rule)
                initial.append(Reminder(str(uuid4()), task.id, rule, str(self._timezone),
                    ReminderStatus.ACTIVE if candidate else ReminderStatus.PAUSED,
                    "event_moved_past" if candidate is None else None, candidate, 1, now, now))
            created = await self._repository.create_with_reminders(task, tuple(initial))
        return self._view(created, now)

    async def get(self, task_id: str) -> PersonalTaskView:
        task = await self._repository.get(task_id)
        if task is None:
            raise PersonalTaskNotFound(task_id)
        return self._view(task, self._clock().astimezone(UTC))

    async def get_detail(self, task_id: str) -> tuple[PersonalTaskView, tuple[Reminder, ...]]:
        task, reminders = await self._repository.get_detail(task_id)
        if task is None:
            raise PersonalTaskNotFound(task_id)
        return self._view(task, self._clock().astimezone(UTC)), reminders

    async def list(self, filter: PersonalTaskFilter = PersonalTaskFilter.ACTIVE) -> PersonalTaskList:
        if not isinstance(filter, PersonalTaskFilter):
            raise InvalidPersonalTaskInput("事务筛选条件无效")
        statuses = {
            PersonalTaskFilter.ACTIVE: (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS),
            PersonalTaskFilter.ALL: None,
            PersonalTaskFilter.COMPLETED: (PersonalTaskStatus.COMPLETED,),
            PersonalTaskFilter.CANCELLED: (PersonalTaskStatus.CANCELLED,),
        }[filter]
        tasks = tuple(sorted(await self._repository.list(statuses), key=lambda t: (
            self._sort_time(t),
            t.created_at, t.id)))
        now = self._clock().astimezone(UTC)
        return PersonalTaskList(tuple(self._view(task, now) for task in tasks), str(self._timezone), now,
                                len(tasks), 0, len(tasks))

    def _sort_time(self, task: PersonalTask):
        primary = task.planning.event_time if task.planning.kind == PersonalTaskKind.EVENT else task.planning.scheduled_time
        deadline = primary or task.deadline
        return (deadline is None, deadline_sort_at(deadline, self._timezone) if deadline else task.created_at)

    async def query(self, query: PersonalTaskQuery) -> PersonalTaskList:
        if not isinstance(query, PersonalTaskQuery):
            raise InvalidPersonalTaskInput("query 必须是 PersonalTaskQuery")
        if not isinstance(query.filter, PersonalTaskFilter) or not isinstance(query.keyword, str):
            raise InvalidPersonalTaskInput("查询条件类型无效")
        if not isinstance(query.plan_filter, PlanFilter):
            raise InvalidPersonalTaskInput("计划筛选条件无效")
        if query.deadline_from is not None and (not isinstance(query.deadline_from, date) or isinstance(query.deadline_from, datetime)):
            raise InvalidPersonalTaskInput("deadline_from 必须是 date")
        if query.deadline_until is not None and (not isinstance(query.deadline_until, date) or isinstance(query.deadline_until, datetime)):
            raise InvalidPersonalTaskInput("deadline_until 必须是 date")
        if query.deadline_from is not None and query.deadline_until is not None and query.deadline_from >= query.deadline_until:
            raise InvalidPersonalTaskInput("deadline_from 必须早于 deadline_until")
        if type(query.overdue_only) is not bool or type(query.offset) is not int or query.offset < 0 or type(query.limit) is not int or not 1 <= query.limit <= 100:
            raise InvalidPersonalTaskInput("分页或逾期筛选条件无效")
        tasks, reminder_summaries = await self._repository.read_task_snapshot()
        now = self._clock().astimezone(UTC)
        tasks = tuple(sorted(tasks, key=lambda t: (*self._sort_time(t), t.created_at, t.id)))
        keyword = query.keyword.strip().casefold()
        filtered = []
        for task in tasks:
            ended = task.status in (PersonalTaskStatus.COMPLETED, PersonalTaskStatus.CANCELLED)
            when = task.planning.event_time if task.planning.kind == PersonalTaskKind.EVENT else task.planning.scheduled_time
            if query.plan_filter == PlanFilter.ENDED and not ended:
                continue
            if query.plan_filter == PlanFilter.UNSCHEDULED and (ended or when is not None):
                continue
            if query.plan_filter == PlanFilter.SCHEDULED and (ended or when is None):
                continue
            if query.filter == PersonalTaskFilter.ACTIVE and task.status not in (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS):
                continue
            if query.filter == PersonalTaskFilter.COMPLETED and task.status != PersonalTaskStatus.COMPLETED:
                continue
            if query.filter == PersonalTaskFilter.CANCELLED and task.status != PersonalTaskStatus.CANCELLED:
                continue
            if keyword and keyword not in task.title.casefold() and keyword not in task.description.casefold():
                continue
            if query.deadline_from is not None or query.deadline_until is not None:
                if task.deadline is None:
                    continue
                local_date = task.deadline.value if task.deadline.kind == "date" else task.deadline.value.astimezone(self._timezone).date()
                if query.deadline_from is not None and local_date < query.deadline_from:
                    continue
                if query.deadline_until is not None and local_date >= query.deadline_until:
                    continue
            if query.overdue_only and not is_overdue(task, now, self._timezone):
                continue
            filtered.append(task)
        total = len(filtered)
        page = tuple(filtered[query.offset:query.offset + query.limit])
        summaries = tuple(reminder_summaries[task.id] for task in page)
        return PersonalTaskList(tuple(self._view(task, now) for task in page), str(self._timezone), now,
                                total, query.offset, query.limit, summaries)

    async def update(self, task_id: str, *, title: str, description: str, deadline: TaskDeadline | None,
                     expected_version: int, planning: TaskPlanning | None = None) -> PersonalTaskView:
        if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or type(expected_version) is not int or expected_version < 1:
            raise InvalidPersonalTaskInput("标题不能为空且 expected_version 必须为正数")
        current = await self._repository.get(task_id)
        if current is None:
            raise PersonalTaskNotFound(task_id)
        if current.version != expected_version:
            raise PersonalTaskVersionConflict(task_id)
        title = title.strip()
        planning = current.planning if planning is None else planning
        await self._validate_planning(planning, task_id)
        now = self._clock().astimezone(UTC)
        updated = PersonalTask(current.id, title, description, current.status, deadline,
                               current.created_at, now, current.completed_at, current.version + 1, planning)
        saved, changes = await self._repository.replace_with_effects(updated, expected_version=expected_version, now=now, timezone=self._timezone)
        return self._view(saved, now, changes)

    async def change_status(self, task_id: str, *, status: PersonalTaskStatus, expected_version: int) -> PersonalTaskView:
        if not isinstance(status, PersonalTaskStatus) or type(expected_version) is not int or expected_version < 1:
            raise InvalidPersonalTaskInput("状态或 expected_version 无效")
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
                               current.version + 1, current.planning)
        saved, changes = await self._repository.replace_with_effects(updated, expected_version=expected_version, now=now, timezone=self._timezone)
        return self._view(saved, now, changes)

    async def delete(self, task_id: str, *, expected_version: int) -> None:
        if type(expected_version) is not int or expected_version < 1:
            raise InvalidPersonalTaskInput("expected_version 必须为正数")
        await self._repository.delete(task_id, expected_version=expected_version)
