"""个人事务提醒规则与实体。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypedDict


class AtRule(TypedDict):
    kind: Literal["at"]
    at: str


class BeforeDeadlineRule(TypedDict):
    kind: Literal["before_deadline"]
    offset_minutes: int


class BeforeEventRule(TypedDict):
    kind: Literal["before_event"]
    offset_minutes: int


class BeforeDateRule(TypedDict):
    kind: Literal["before_date"]
    days_before: int
    local_time: str


class DailyRule(TypedDict):
    kind: Literal["daily"]
    local_time: str


class WeeklyRule(TypedDict):
    kind: Literal["weekly"]
    weekdays: list[int]
    local_time: str


class MonthlyRule(TypedDict):
    kind: Literal["monthly"]
    day: int
    local_time: str


class YearlyRule(TypedDict):
    kind: Literal["yearly"]
    month: int
    day: int
    local_time: str


ReminderRule = AtRule | BeforeDeadlineRule | BeforeEventRule | BeforeDateRule | DailyRule | WeeklyRule | MonthlyRule | YearlyRule


class ReminderStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FIRED = "fired"
    MISSED = "missed"


@dataclass(frozen=True)
class Reminder:
    id: str
    task_id: str
    rule: ReminderRule
    timezone: str
    status: ReminderStatus
    reason: str | None
    next_fire_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReminderOccurrence:
    id: str
    reminder_id: str
    task_id: str
    reminder_version: int
    scheduled_at: datetime
    processed_at: datetime
    outcome: Literal["fired", "missed"]
    missed_until: datetime | None
