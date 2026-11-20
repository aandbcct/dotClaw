"""个人事务仓储接口与业务异常。"""

from datetime import datetime
from typing import Protocol, Literal
from zoneinfo import ZoneInfo

from .models import PersonalTask, PersonalTaskStatus
from .reminder_models import Reminder
from .notification_models import Notification, NotificationPage, NotificationChanges, NotificationDelivery


class PersonalTaskNotFound(Exception):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"事务不存在：{task_id}")


class PersonalTaskVersionConflict(Exception):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"事务版本冲突：{task_id}")


class PersonalTaskStorageError(Exception):
    pass


class ReminderNotFound(Exception):
    pass


class ReminderVersionConflict(Exception):
    pass


class InvalidReminderInput(ValueError):
    pass


class InvalidReminderTransition(Exception):
    pass


class NotificationNotFound(Exception):
    pass


class NotificationDeliveryConflict(Exception):
    pass


class PersonalTaskRepository(Protocol):
    async def create(self, task: PersonalTask) -> PersonalTask: ...
    async def create_with_reminders(self, task: PersonalTask, reminders: tuple[Reminder, ...]) -> PersonalTask: ...
    async def get(self, task_id: str) -> PersonalTask | None: ...
    async def list(self, statuses: tuple[PersonalTaskStatus, ...] | None) -> tuple[PersonalTask, ...]: ...
    async def replace(self, task: PersonalTask, *, expected_version: int, now: datetime | None = None, timezone: ZoneInfo | None = None) -> PersonalTask: ...
    async def delete(self, task_id: str, *, expected_version: int) -> None: ...
    async def list_reminders(self, task_id: str) -> tuple[Reminder, ...]: ...
    async def get_reminder(self, task_id: str, reminder_id: str) -> Reminder: ...
    async def create_reminder(self, reminder: Reminder, *, expected_task_version: int) -> Reminder: ...
    async def replace_reminder(self, reminder: Reminder, *, expected_version: int, expected_task_version: int) -> Reminder: ...
    async def process_due(self, *, now: datetime, timezone: ZoneInfo, startup: bool) -> None: ...
    async def get_scan_time(self) -> datetime | None: ...
    async def list_notifications(self, *, unread_only: bool, before_id: int | None, limit: int) -> NotificationPage: ...
    async def get_notification(self, notification_id: int) -> Notification: ...
    async def notification_changes(self, *, after_id: int, limit: int) -> NotificationChanges: ...
    async def mark_notification_read(self, notification_id: int, *, now: datetime) -> Notification: ...
    async def claim_notification_delivery(self, notification_id: int, *, channel: str, claim_id: str, now: datetime) -> NotificationDelivery | None: ...
    async def report_notification_delivery(self, notification_id: int, *, claim_id: str, status: str, error: str | None, now: datetime) -> NotificationDelivery: ...
