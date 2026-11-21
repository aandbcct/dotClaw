"""站内通知及浏览器投递认领服务。"""

from datetime import UTC, datetime
from typing import Callable, Literal
from uuid import uuid4

from .notification_models import Notification, NotificationChanges, NotificationDelivery, NotificationPage
from .repository import InvalidReminderInput, NotificationDeliveryConflict, PersonalTaskRepository


class NotificationService:
    def __init__(self, repository: PersonalTaskRepository, *, clock: Callable[[], datetime] | None = None):
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list(self, *, unread_only: bool = False, before_id: int | None = None, limit: int = 50) -> NotificationPage:
        return await self._repository.list_notifications(unread_only=unread_only, before_id=before_id, limit=limit)

    async def get(self, notification_id: int) -> Notification:
        return await self._repository.get_notification(notification_id)

    async def changes(self, *, after_id: int, limit: int = 100) -> NotificationChanges:
        return await self._repository.notification_changes(after_id=after_id, limit=limit)

    async def mark_read(self, notification_id: int) -> Notification:
        return await self._repository.mark_notification_read(notification_id, now=self._clock().astimezone(UTC))

    async def claim_delivery(self, notification_id: int, *, channel: str) -> NotificationDelivery | None:
        if channel != "browser":
            raise InvalidReminderInput("仅支持 browser 投递")
        return await self._repository.claim_notification_delivery(notification_id, channel=channel, claim_id=str(uuid4()), now=self._clock().astimezone(UTC))

    async def report_delivery(self, notification_id: int, *, claim_id: str, status: Literal["accepted", "failed"], error: str | None = None) -> NotificationDelivery:
        if status == "accepted" and error:
            raise InvalidReminderInput("accepted 不能包含 error")
        return await self._repository.report_notification_delivery(notification_id, claim_id=claim_id, status=status, error=error[:500] if error else None, now=self._clock().astimezone(UTC))
