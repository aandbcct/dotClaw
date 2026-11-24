"""个人事务领域模型与时间规则。"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo


class PersonalTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PersonalTaskFilter(StrEnum):
    ACTIVE = "active"
    ALL = "all"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PersonalTaskKind(StrEnum):
    TASK = "task"
    EVENT = "event"


class PlanFilter(StrEnum):
    ALL = "all"
    UNSCHEDULED = "unscheduled"
    SCHEDULED = "scheduled"
    ENDED = "ended"


@dataclass(frozen=True)
class TaskDeadline:
    kind: Literal["date", "datetime"]
    value: date | datetime

    def __post_init__(self) -> None:
        if self.kind == "date":
            if not isinstance(self.value, date) or isinstance(self.value, datetime):
                raise ValueError("日期截止值必须是 date")
        elif self.kind == "datetime":
            if not isinstance(self.value, datetime) or self.value.tzinfo is None or self.value.utcoffset() is None:
                raise ValueError("时刻截止值必须带时区")
            object.__setattr__(self, "value", self.value.astimezone(UTC))
        else:
            raise ValueError("不支持的截止类型")


@dataclass(frozen=True)
class TaskPlanning:
    kind: PersonalTaskKind = PersonalTaskKind.TASK
    event_time: TaskDeadline | None = None
    scheduled_time: TaskDeadline | None = None
    time_text: str = ""
    related_task_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PersonalTaskKind) or not isinstance(self.time_text, str):
            raise ValueError("事务安排字段类型无效")
        if self.event_time is not None and not isinstance(self.event_time, TaskDeadline):
            raise ValueError("event_time 必须是 TaskDeadline")
        if self.scheduled_time is not None and not isinstance(self.scheduled_time, TaskDeadline):
            raise ValueError("scheduled_time 必须是 TaskDeadline")
        if self.related_task_id is not None and not isinstance(self.related_task_id, str):
            raise ValueError("related_task_id 必须是字符串或 None")
        if self.kind == PersonalTaskKind.EVENT and self.scheduled_time is not None:
            raise ValueError("事件不能填写任务执行时间")
        if self.kind == PersonalTaskKind.TASK and self.event_time is not None:
            raise ValueError("任务不能填写事件时间")


@dataclass(frozen=True)
class PersonalTask:
    id: str
    title: str
    description: str
    status: PersonalTaskStatus
    deadline: TaskDeadline | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int
    planning: TaskPlanning = field(default_factory=TaskPlanning)


@dataclass(frozen=True)
class PersonalTaskView:
    task: PersonalTask
    is_overdue: bool
    reminder_changes: tuple["ReminderChange", ...] = ()


@dataclass(frozen=True)
class ReminderChange:
    reminder_id: str
    action: Literal["rescheduled", "paused", "cancelled"]
    reason: str | None
    next_fire_at: datetime | None
    version: int


@dataclass(frozen=True)
class PersonalTaskQuery:
    filter: PersonalTaskFilter = PersonalTaskFilter.ACTIVE
    keyword: str = ""
    deadline_from: date | None = None
    deadline_until: date | None = None
    overdue_only: bool = False
    offset: int = 0
    limit: int = 50
    plan_filter: PlanFilter = PlanFilter.ALL


@dataclass(frozen=True)
class ReminderSummary:
    active_count: int
    next_fire_at: datetime | None


@dataclass(frozen=True)
class PersonalTaskList:
    items: tuple[PersonalTaskView, ...]
    timezone: str
    as_of: datetime
    total: int = 0
    offset: int = 0
    limit: int = 50
    reminder_summaries: tuple[ReminderSummary, ...] = ()


def deadline_sort_at(deadline: TaskDeadline, zone: ZoneInfo) -> datetime:
    if deadline.kind == "datetime":
        return deadline.value  # type: ignore[return-value]
    next_day = deadline.value + timedelta(days=1)
    return datetime.combine(next_day, time.min, tzinfo=zone).astimezone(UTC)


def is_overdue(task: PersonalTask, now: datetime, zone: ZoneInfo) -> bool:
    if task.status not in (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS) or task.deadline is None:
        return False
    if task.deadline.kind == "date":
        return now.astimezone(zone).date() > task.deadline.value  # type: ignore[operator]
    return now > task.deadline.value  # type: ignore[operator]
