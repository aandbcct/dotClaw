"""提醒 API 的严格请求与响应模型。"""

import re
import calendar
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AtRulePayload(_Rule):
    kind: Literal["at"]
    at: str

    @field_validator("at")
    @classmethod
    def rfc3339(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("at 必须是带偏移 RFC3339 时刻")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class BeforeDeadlineRulePayload(_Rule):
    kind: Literal["before_deadline"]
    offset_minutes: Annotated[int, Field(ge=0)]


class BeforeDateRulePayload(_Rule):
    kind: Literal["before_date"]
    days_before: Annotated[int, Field(ge=0)]
    local_time: str

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", value) or int(value[:2]) > 23 or int(value[3:]) > 59:
            raise ValueError("local_time 必须为 HH:mm")
        return value


class DailyRulePayload(_Rule):
    kind: Literal["daily"]
    local_time: str

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", value) or int(value[:2]) > 23 or int(value[3:]) > 59:
            raise ValueError("local_time 必须为 HH:mm")
        return value


class WeeklyRulePayload(_Rule):
    kind: Literal["weekly"]
    weekdays: list[Annotated[int, Field(ge=0, le=6)]]
    local_time: str

    @field_validator("weekdays")
    @classmethod
    def valid_weekdays(cls, value: list[int]) -> list[int]:
        if not value or value != sorted(set(value)):
            raise ValueError("weekdays 必须是非空去重升序列表")
        return value

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", value) or int(value[:2]) > 23 or int(value[3:]) > 59:
            raise ValueError("local_time 必须为 HH:mm")
        return value


class MonthlyRulePayload(_Rule):
    kind: Literal["monthly"]
    day: Annotated[int, Field(ge=1, le=31)]
    local_time: str

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", value) or int(value[:2]) > 23 or int(value[3:]) > 59:
            raise ValueError("local_time 必须为 HH:mm")
        return value


class YearlyRulePayload(_Rule):
    kind: Literal["yearly"]
    month: Annotated[int, Field(ge=1, le=12)]
    day: Annotated[int, Field(ge=1, le=31)]
    local_time: str

    @field_validator("day")
    @classmethod
    def valid_day(cls, value: int, info) -> int:
        month = info.data.get("month")
        if month and value > calendar.monthrange(2000, month)[1]:
            raise ValueError("yearly 月日无效")
        return value

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", value) or int(value[:2]) > 23 or int(value[3:]) > 59:
            raise ValueError("local_time 必须为 HH:mm")
        return value


ReminderRulePayload = Annotated[AtRulePayload | BeforeDeadlineRulePayload | BeforeDateRulePayload | DailyRulePayload | WeeklyRulePayload | MonthlyRulePayload | YearlyRulePayload, Field(discriminator="kind")]


class CreateReminderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rule: ReminderRulePayload
    expected_task_version: Annotated[int, Field(ge=1)]


class UpdateReminderRequest(CreateReminderRequest):
    expected_version: Annotated[int, Field(ge=1)]


class ReminderStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["pause", "resume", "cancel"]
    expected_version: Annotated[int, Field(ge=1)]
    expected_task_version: Annotated[int, Field(ge=1)]


class ReminderResponse(BaseModel):
    id: str
    task_id: str
    rule: dict
    timezone: str
    status: str
    reason: str | None
    next_fire_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class ReminderListResponse(BaseModel):
    items: list[ReminderResponse]
    timezone: str
    scheduler_enabled: bool
    last_scan_at: datetime | None
    scan_lag_seconds: float | None
