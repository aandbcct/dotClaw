"""个人事务领域模型与时间规则。"""

from dataclasses import dataclass
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


@dataclass(frozen=True)
class TaskDeadline:
    kind: Literal["date", "datetime"]
    value: date | datetime

    def __post_init__(self) -> None:
        if self.kind == "date":
            if not isinstance(self.value, date) or isinstance(self.value, datetime):
                raise ValueError("日期截止值必须是 date")
        elif self.kind == "datetime":
            if not isinstance(self.value, datetime) or self.value.tzinfo is None:
                raise ValueError("时刻截止值必须带时区")
            object.__setattr__(self, "value", self.value.astimezone(UTC))
        else:
            raise ValueError("不支持的截止类型")


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


@dataclass(frozen=True)
class PersonalTaskView:
    task: PersonalTask
    is_overdue: bool


@dataclass(frozen=True)
class PersonalTaskList:
    items: tuple[PersonalTaskView, ...]
    timezone: str
    as_of: datetime


def deadline_sort_at(deadline: TaskDeadline, zone: ZoneInfo) -> datetime:
    if deadline.kind == "datetime":
        return deadline.value  # type: ignore[return-value]
    next_day = deadline.value + timedelta(days=1)
    return datetime.combine(next_day, time.min, tzinfo=zone, fold=0).astimezone(UTC)


def is_overdue(task: PersonalTask, now: datetime, zone: ZoneInfo) -> bool:
    if task.status not in (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS) or task.deadline is None:
        return False
    if task.deadline.kind == "date":
        return now.astimezone(zone).date() > task.deadline.value  # type: ignore[operator]
    return now > task.deadline.value  # type: ignore[operator]
