"""个人事务提醒的应用服务。"""

from datetime import UTC, datetime
from typing import Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import PersonalTaskStatus
from .reminder_models import Reminder, ReminderRule, ReminderStatus
from .reminder_rules import next_fire_at, validate_rule
from .repository import InvalidReminderInput, InvalidReminderTransition, PersonalTaskNotFound, PersonalTaskRepository, ReminderNotFound


class ReminderService:
    def __init__(self, repository: PersonalTaskRepository, *, timezone: ZoneInfo, clock: Callable[[], datetime] | None = None):
        self._repository = repository
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list(self, task_id: str) -> tuple[Reminder, ...]:
        if await self._repository.get(task_id) is None:
            raise PersonalTaskNotFound(task_id)
        return await self._repository.list_reminders(task_id)

    async def create(self, task_id: str, *, rule: ReminderRule, expected_task_version: int) -> Reminder:
        task = await self._repository.get(task_id)
        if task is None:
            raise PersonalTaskNotFound(task_id)
        if task.version != expected_task_version:
            from .repository import PersonalTaskVersionConflict
            raise PersonalTaskVersionConflict(task_id)
        if task.status not in (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS):
            raise InvalidReminderTransition("已结束事务不能新增提醒")
        now = self._clock().astimezone(UTC)
        try:
            validate_rule(rule, task, self._timezone)
            candidate = next_fire_at(rule, task, self._timezone, now)
        except (ValueError, KeyError, TypeError) as error:
            raise InvalidReminderInput(str(error)) from error
        if candidate is None:
            raise InvalidReminderInput("提醒时间必须晚于当前时间")
        if rule["kind"] == "at":
            rule = {**rule, "at": datetime.fromisoformat(rule["at"].replace("Z", "+00:00")).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")}
        reminder = Reminder(str(uuid4()), task_id, rule, str(self._timezone), ReminderStatus.ACTIVE, None,
                            candidate, 1, now, now)
        return await self._repository.create_reminder(reminder, expected_task_version=expected_task_version)

    async def update(self, task_id: str, reminder_id: str, *, rule: ReminderRule, expected_version: int, expected_task_version: int) -> Reminder:
        task = await self._repository.get(task_id)
        if task is None:
            raise PersonalTaskNotFound(task_id)
        if task.version != expected_task_version:
            from .repository import PersonalTaskVersionConflict
            raise PersonalTaskVersionConflict(task_id)
        current = await self._repository.get_reminder(task_id, reminder_id)
        if current.version != expected_version:
            from .repository import ReminderVersionConflict
            raise ReminderVersionConflict(reminder_id)
        if current.status not in (ReminderStatus.ACTIVE, ReminderStatus.PAUSED):
            raise InvalidReminderTransition("终态提醒不能修改")
        now = self._clock().astimezone(UTC)
        try:
            validate_rule(rule, task, self._timezone)
            candidate = next_fire_at(rule, task, self._timezone, now)
        except (ValueError, KeyError, TypeError) as error:
            raise InvalidReminderInput(str(error)) from error
        if candidate is None:
            raise InvalidReminderInput("提醒时间必须晚于当前时间")
        if rule["kind"] == "at":
            rule = {**rule, "at": datetime.fromisoformat(rule["at"].replace("Z", "+00:00")).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")}
        updated = Reminder(current.id, task_id, rule, str(self._timezone), ReminderStatus.ACTIVE, None,
                           candidate, current.version + 1, current.created_at, now)
        return await self._repository.replace_reminder(updated, expected_version=expected_version, expected_task_version=expected_task_version)

    async def change_status(self, task_id: str, reminder_id: str, *, action: Literal["pause", "resume", "cancel"], expected_version: int, expected_task_version: int) -> Reminder:
        task = await self._repository.get(task_id)
        if task is None:
            raise PersonalTaskNotFound(task_id)
        current = await self._repository.get_reminder(task_id, reminder_id)
        if current.version != expected_version:
            from .repository import ReminderVersionConflict
            raise ReminderVersionConflict(reminder_id)
        now = self._clock().astimezone(UTC)
        if action == "pause" and current.status == ReminderStatus.ACTIVE and current.rule["kind"] in ("daily", "weekly", "monthly", "yearly"):
            result = Reminder(current.id, task_id, current.rule, current.timezone, ReminderStatus.PAUSED, None, None, current.version + 1, current.created_at, now)
        elif action == "resume" and current.status == ReminderStatus.PAUSED and task.status in (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS):
            try:
                rule_zone = ZoneInfo(current.timezone)
                candidate = next_fire_at(current.rule, task, rule_zone, now)
            except (ValueError, KeyError, TypeError) as error:
                raise InvalidReminderInput(str(error)) from error
            if candidate is None:
                raise InvalidReminderInput("提醒规则没有未来时刻")
            result = Reminder(current.id, task_id, current.rule, current.timezone, ReminderStatus.ACTIVE, None, candidate, current.version + 1, current.created_at, now)
        elif action == "cancel" and current.status in (ReminderStatus.ACTIVE, ReminderStatus.PAUSED):
            result = Reminder(current.id, task_id, current.rule, current.timezone, ReminderStatus.CANCELLED, None, None, current.version + 1, current.created_at, now)
        else:
            raise InvalidReminderTransition("当前提醒状态不允许此操作")
        return await self._repository.replace_reminder(result, expected_version=expected_version, expected_task_version=expected_task_version)

    async def scan(self, *, startup: bool = False) -> None:
        await self._repository.process_due(now=self._clock().astimezone(UTC), timezone=self._timezone, startup=startup)

    async def last_scan_at(self) -> datetime | None:
        return await self._repository.get_scan_time()
