"""提醒规则的校验与日历时刻计算。"""

import calendar
from datetime import UTC, date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .models import PersonalTask
from .reminder_models import Reminder, ReminderRule, ReminderStatus


def _local_candidates(rule: ReminderRule, zone: ZoneInfo, after: datetime) -> datetime | None:
    local_after = after.astimezone(zone)
    values: dict[str, object] = {"timezone": zone}
    kind = rule["kind"]
    if kind == "daily":
        hour, minute = map(int, rule["local_time"].split(":"))
        values.update(hour=hour, minute=minute, second=0)
    elif kind == "weekly":
        hour, minute = map(int, rule["local_time"].split(":"))
        values.update(day_of_week=",".join(str(day) for day in rule["weekdays"]), hour=hour, minute=minute, second=0)
    elif kind == "monthly":
        hour, minute = map(int, rule["local_time"].split(":"))
        values.update(day=rule["day"], hour=hour, minute=minute, second=0)
    elif kind == "yearly":
        hour, minute = map(int, rule["local_time"].split(":"))
        values.update(month=rule["month"], day=rule["day"], hour=hour, minute=minute, second=0)
    else:
        raise ValueError("非周期规则不能使用日历触发器")
    trigger = CronTrigger(**values)
    cursor = local_after
    while True:
        candidate = trigger.get_next_fire_time(None, cursor)
        if candidate is None:
            return None
        utc_candidate = candidate.astimezone(UTC)
        roundtrip = utc_candidate.astimezone(zone)
        if roundtrip.replace(tzinfo=None) == candidate.replace(tzinfo=None) and candidate.fold == 0 and utc_candidate > after:
            return utc_candidate
        cursor = candidate + timedelta(seconds=1)


def validate_rule(rule: ReminderRule, task: PersonalTask, zone: ZoneInfo) -> None:
    fields = {"at": {"kind", "at"}, "before_deadline": {"kind", "offset_minutes"},
              "before_date": {"kind", "days_before", "local_time"}, "daily": {"kind", "local_time"},
              "weekly": {"kind", "weekdays", "local_time"}, "monthly": {"kind", "day", "local_time"},
              "yearly": {"kind", "month", "day", "local_time"}}
    if not isinstance(rule, dict) or not isinstance(rule.get("kind"), str) or rule["kind"] not in fields or set(rule) != fields[rule["kind"]]:
        raise ValueError("提醒规则字段无效")
    kind = rule["kind"]
    if kind == "at":
        if not isinstance(rule["at"], str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", rule["at"]) is None:
            raise ValueError("at 必须是带时区的 RFC3339 字符串")
        value = datetime.fromisoformat(rule["at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("at 必须包含时区偏移")
    elif kind == "before_deadline":
        if type(rule["offset_minutes"]) is not int or task.deadline is None or task.deadline.kind != "datetime" or rule["offset_minutes"] < 0:
            raise ValueError("before_deadline 需要时刻截止和非负偏移")
    elif kind == "before_date":
        if type(rule["days_before"]) is not int or task.deadline is None or task.deadline.kind != "date" or rule["days_before"] < 0:
            raise ValueError("before_date 需要日期截止和非负天数")
        _validate_local_time(rule["local_time"])
    else:
        _validate_local_time(rule["local_time"])
        if kind == "weekly":
            if not isinstance(rule["weekdays"], list) or any(type(day) is not int for day in rule["weekdays"]):
                raise ValueError("weekly weekdays 类型无效")
            if not rule["weekdays"] or sorted(set(rule["weekdays"])) != rule["weekdays"] or any(day not in range(7) for day in rule["weekdays"]):
                raise ValueError("weekly weekdays 必须是去重升序的 0..6")
        if kind == "monthly" and (type(rule["day"]) is not int or rule["day"] not in range(1, 32)):
            raise ValueError("monthly day 必须为 1..31")
        if kind == "yearly" and (type(rule["month"]) is not int or type(rule["day"]) is not int or rule["month"] not in range(1, 13) or rule["day"] not in range(1, calendar.monthrange(2000, rule["month"])[1] + 1)):
            raise ValueError("yearly 月日无效")


def _validate_local_time(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError("当地时刻必须为 HH:MM")


def next_fire_at(rule: ReminderRule, task: PersonalTask, zone: ZoneInfo, after: datetime) -> datetime | None:
    after = after.astimezone(UTC)
    kind = rule["kind"]
    if kind == "at":
        candidate = DateTrigger(run_date=datetime.fromisoformat(rule["at"].replace("Z", "+00:00"))).run_date
    elif kind == "before_deadline":
        candidate = task.deadline.value - timedelta(minutes=rule["offset_minutes"])
    elif kind == "before_date":
        local_day = task.deadline.value - timedelta(days=rule["days_before"])
        hour, minute = map(int, rule["local_time"].split(":"))
        local = datetime.combine(local_day, time(hour, minute), zone)
        candidate = local.astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("提醒时刻在时区切换时不存在")
    else:
        return _local_candidates(rule, zone, after)
    return candidate.astimezone(UTC) if candidate.astimezone(UTC) > after else None


def reschedule_relative(reminder: Reminder, task: PersonalTask, now: datetime) -> Reminder:
    if reminder.rule["kind"] not in ("before_deadline", "before_date"):
        return reminder
    zone = ZoneInfo(reminder.timezone)
    try:
        candidate = next_fire_at(reminder.rule, task, zone, now)
    except ValueError:
        candidate = None
        reason = "invalid_local_time"
    else:
        reason = "deadline_moved_past" if candidate is None else None
    return Reminder(reminder.id, reminder.task_id, reminder.rule, reminder.timezone,
                    ReminderStatus.ACTIVE if candidate else ReminderStatus.PAUSED, reason,
                    candidate, reminder.version + 1, reminder.created_at, now)
