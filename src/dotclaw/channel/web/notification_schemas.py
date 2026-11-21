"""通知 API 的请求与响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NotificationMemberResponse(BaseModel):
    task_id: str
    title_snapshot: str
    missed: bool
    overdue: bool
    current_status: str | None
    current_is_overdue: bool | None


class NotificationDeliveryResponse(BaseModel):
    notification_id: int
    channel: str
    claim_id: str
    status: str
    attempted_at: datetime
    reported_at: datetime | None
    error: str | None


class NotificationResponse(BaseModel):
    id: int
    kind: str
    occurrence_id: str | None
    created_at: datetime
    read_at: datetime | None
    title: str
    body: str
    members: list[NotificationMemberResponse]
    deliveries: list[NotificationDeliveryResponse]


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    next_before_id: int | None
    unread_count: int
    watermark: int


class NotificationChangesResponse(BaseModel):
    items: list[NotificationResponse]
    next_after_id: int
    has_more: bool
    unread_count: int
    watermark: int


class ClaimDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel: Literal["browser"]


class ReportDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    claim_id: str
    status: Literal["accepted", "failed"]
    error: str | None = None
