"""个人事务 HTTP 请求与响应模型。"""

from datetime import date, datetime, UTC
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from ...personal_tasks.models import PersonalTask, PersonalTaskFilter, PersonalTaskStatus, PersonalTaskView, TaskDeadline


class DateDeadlinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["date"]
    value: date

    @field_validator("value", mode="before")
    @classmethod
    def parse_date(cls, value):
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("日期必须为 YYYY-MM-DD")
        return date.fromisoformat(value)


class DateTimeDeadlinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["datetime"]
    value: datetime

    @field_validator("value", mode="before")
    @classmethod
    def parse_datetime(cls, value):
        if not isinstance(value, str):
            raise ValueError("截止时刻必须为 RFC3339 字符串")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("截止时刻必须包含时区")
        return parsed.astimezone(UTC)


TaskDeadlinePayload = Annotated[DateDeadlinePayload | DateTimeDeadlinePayload, Field(discriminator="kind")]
_deadline_adapter = TypeAdapter(TaskDeadlinePayload)


class CreatePersonalTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    description: str = ""
    deadline: TaskDeadlinePayload | None = None


class UpdatePersonalTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    description: str
    deadline: TaskDeadlinePayload | None
    expected_version: Annotated[int, Field(ge=1, strict=True)]


class ChangePersonalTaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: PersonalTaskStatus
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


class PersonalTaskListResponse(BaseModel):
    items: list[PersonalTaskResponse]
    timezone: str
    as_of: datetime


def deadline_to_domain(value: TaskDeadlinePayload | None) -> TaskDeadline | None:
    if value is None:
        return None
    return TaskDeadline(value.kind, value.value)


def to_task_response(view: PersonalTaskView) -> PersonalTaskResponse:
    task = view.task
    deadline = _deadline_adapter.validate_python({"kind": task.deadline.kind, "value": task.deadline.value}) if task.deadline else None
    return PersonalTaskResponse(id=task.id, title=task.title, description=task.description, status=task.status,
        deadline=deadline, created_at=task.created_at, updated_at=task.updated_at,
        completed_at=task.completed_at, version=task.version, is_overdue=view.is_overdue)


def to_task_list_response(result) -> PersonalTaskListResponse:
    return PersonalTaskListResponse(items=[to_task_response(item) for item in result.items], timezone=result.timezone, as_of=result.as_of)
