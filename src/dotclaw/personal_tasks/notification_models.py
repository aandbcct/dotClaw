"""通知与网页投递事实。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class NotificationMember:
    task_id: str
    title_snapshot: str
    missed: bool
    overdue: bool
    current_status: str | None = None
    current_is_overdue: bool | None = None


@dataclass(frozen=True)
class NotificationDelivery:
    notification_id: int
    channel: str
    claim_id: str
    status: Literal["attempted", "accepted", "failed"]
    attempted_at: datetime
    reported_at: datetime | None
    error: str | None


@dataclass(frozen=True)
class Notification:
    id: int
    kind: Literal["reminder", "recovery_summary"]
    occurrence_id: str | None
    created_at: datetime
    read_at: datetime | None
    title: str
    body: str
    members: tuple[NotificationMember, ...]
    deliveries: tuple[NotificationDelivery, ...] = ()


@dataclass(frozen=True)
class NotificationPage:
    items: tuple[Notification, ...]
    next_before_id: int | None
    unread_count: int
    watermark: int


@dataclass(frozen=True)
class NotificationChanges:
    items: tuple[Notification, ...]
    next_after_id: int
    has_more: bool
    unread_count: int
    watermark: int
