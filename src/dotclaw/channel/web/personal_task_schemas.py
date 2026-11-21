"""个人事务 HTTP 请求与响应模型。"""

from datetime import date, datetime, UTC
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...personal_tasks.models import PersonalTaskStatus, PersonalTaskView, TaskDeadline
from ...personal_tasks.reminder_models import Reminder
from .reminder_schemas import ReminderResponse, ReminderRulePayload


class DateDeadlinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["date"]
    value: date

    @field_validator("value", mode="before")
    @classmethod
    def parse_date(cls, value):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("日期必须为 YYYY-MM-DD")
        return date.fromisoformat(value)


class DateTimeDeadlinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["datetime"]
    value: datetime

    @field_validator("value", mode="before")
    @classmethod
    def parse_datetime(cls, value):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value) is None:
            raise ValueError("截止时刻必须为 RFC3339 字符串")
        return datetime.fromisoformat(value).astimezone(UTC)


TaskDeadlinePayload = Annotated[DateDeadlinePayload | DateTimeDeadlinePayload, Field(discriminator="kind")]


class CreatePersonalTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    description: str = ""
    deadline: TaskDeadlinePayload | None = None
    reminders: list[ReminderRulePayload] = Field(default_factory=list)


class UpdatePersonalTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    description: str
    deadline: TaskDeadlinePayload | None
    expected_version: Annotated[int, Field(ge=1, strict=True)]


class ChangePersonalTaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: PersonalTaskStatus = Field(strict=False)
    expected_version: Annotated[int, Field(ge=1, strict=True)]


class PersonalTaskResponse(BaseModel):
    id: str
    title: str
    description: str
    status: PersonalTaskStatus
    deadline: TaskDeadlinePayload | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int
    is_overdue: bool
    reminder_changes: list["ReminderChangeResponse"] = Field(default_factory=list)


class ReminderChangeResponse(BaseModel):
    reminder_id: str
    action: Literal["rescheduled", "paused", "cancelled"]
    reason: str | None
    next_fire_at: datetime | None
    version: int


class PersonalTaskDetailResponse(PersonalTaskResponse):
    reminders: list[ReminderResponse]


class ReminderSummaryResponse(BaseModel):
    active_count: int
    next_fire_at: datetime | None


class PersonalTaskListItemResponse(PersonalTaskResponse):
    reminder_summary: ReminderSummaryResponse


class PersonalTaskListResponse(BaseModel):
    items: list[PersonalTaskListItemResponse]
    timezone: str
    as_of: datetime
    total: int
    offset: int
    limit: int
    has_more: bool


def deadline_to_domain(value: TaskDeadlinePayload | None) -> TaskDeadline | None:
    if value is None:
        return None
    return TaskDeadline(value.kind, value.value)


def to_task_response(view: PersonalTaskView) -> PersonalTaskResponse:
    task = view.task
    deadline = None
    if task.deadline is not None:
        # 领域层已校验截止值，响应直接序列化，避免再次走请求解析。
        if task.deadline.kind == "date":
            deadline = DateDeadlinePayload.model_construct(kind="date", value=task.deadline.value)
        else:
            deadline = DateTimeDeadlinePayload.model_construct(kind="datetime", value=task.deadline.value)
    return PersonalTaskResponse(id=task.id, title=task.title, description=task.description, status=task.status,
        deadline=deadline, created_at=task.created_at, updated_at=task.updated_at,
        completed_at=task.completed_at, version=task.version, is_overdue=view.is_overdue,
        reminder_changes=[ReminderChangeResponse(reminder_id=item.reminder_id, action=item.action, reason=item.reason,
            next_fire_at=item.next_fire_at, version=item.version) for item in view.reminder_changes])


def to_reminder_response(reminder: Reminder) -> ReminderResponse:
    return ReminderResponse.model_construct(id=reminder.id, task_id=reminder.task_id, rule=reminder.rule,
        timezone=reminder.timezone, status=reminder.status, reason=reminder.reason,
        next_fire_at=reminder.next_fire_at, version=reminder.version, created_at=reminder.created_at,
        updated_at=reminder.updated_at)
