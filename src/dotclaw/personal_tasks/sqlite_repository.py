"""个人事务 SQLite 持久化适配器。"""

import asyncio
import calendar
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
import json
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import PersonalTask, PersonalTaskStatus, ReminderChange, ReminderSummary, TaskDeadline, TaskPlanning, PersonalTaskKind
from .repository import PersonalTaskNotFound, PersonalTaskStorageError, PersonalTaskVersionConflict
from .reminder_models import Reminder, ReminderRule, ReminderStatus
from .notification_models import Notification, NotificationMember, NotificationDelivery, NotificationPage, NotificationChanges
from .repository import ReminderNotFound, ReminderVersionConflict, NotificationNotFound, NotificationDeliveryConflict
from .reminder_rules import next_fire_at, reschedule_relative
from .models import is_overdue, PersonalTaskStatus


class SqlitePersonalTaskRepository:
    def __init__(self, db_path: Path, timezone: ZoneInfo = ZoneInfo("UTC"), *, connection: sqlite3.Connection | None = None):
        self._db_path = db_path
        self._timezone = timezone
        self._connection = connection

    @contextmanager
    def _connection_scope(self, *, write: bool = False, snapshot: bool = False):
        if self._connection is None:
            owned = True
            connection = self._connect()
        else:
            owned = False
            connection = self._connection
        try:
            if owned and write:
                connection.execute("BEGIN IMMEDIATE")
            elif owned and snapshot:
                connection.execute("BEGIN")
            yield connection
            if owned and connection.in_transaction:
                connection.execute("COMMIT")
        except BaseException:
            if owned and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if owned:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = None
        try:
            connection = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise PersonalTaskStorageError("打开事务数据库失败") from error

    @staticmethod
    def _stamp(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None

    @staticmethod
    def _date(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) if value else None

    @classmethod
    def _row_to_reminder(cls, row: sqlite3.Row) -> Reminder:
        rule = json.loads(row["rule_json"])
        fields = {"at": {"kind", "at"}, "before_deadline": {"kind", "offset_minutes"}, "before_event": {"kind", "offset_minutes"}, "before_date": {"kind", "days_before", "local_time"}, "daily": {"kind", "local_time"}, "weekly": {"kind", "weekdays", "local_time"}, "monthly": {"kind", "day", "local_time"}, "yearly": {"kind", "month", "day", "local_time"}}
        if not isinstance(rule, dict) or rule.get("kind") not in fields or set(rule) != fields[rule["kind"]]:
            raise ValueError("数据库中的提醒规则结构无效")
        if "local_time" in rule and (not isinstance(rule["local_time"], str) or re.fullmatch(r"\d{2}:[0-5]\d", rule["local_time"]) is None or int(rule["local_time"][:2]) > 23):
            raise ValueError("数据库中的提醒当地时刻无效")
        if rule["kind"] == "at" and (not isinstance(rule["at"], str) or datetime.fromisoformat(rule["at"].replace("Z", "+00:00")).tzinfo is None):
            raise ValueError("数据库中的提醒时刻无效")
        if rule["kind"] in ("before_deadline", "before_event", "before_date") and (type(rule["offset_minutes"] if rule["kind"] != "before_date" else rule["days_before"]) is not int or (rule["offset_minutes"] if rule["kind"] != "before_date" else rule["days_before"]) < 0):
            raise ValueError("数据库中的相对提醒偏移无效")
        if rule["kind"] in ("monthly", "yearly"):
            if type(rule["day"]) is not int or not 1 <= rule["day"] <= 31:
                raise ValueError("数据库中的提醒日期无效")
            if rule["kind"] == "yearly" and (type(rule["month"]) is not int or not 1 <= rule["month"] <= 12 or rule["day"] > calendar.monthrange(2000, rule["month"])[1]):
                raise ValueError("数据库中的年提醒日期无效")
        if rule["kind"] == "weekly" and (not isinstance(rule["weekdays"], list) or not rule["weekdays"] or any(type(day) is not int or not 0 <= day <= 6 for day in rule["weekdays"]) or rule["weekdays"] != sorted(set(rule["weekdays"]))):
            raise ValueError("数据库中的星期规则无效")
        status = ReminderStatus(row["status"])
        next_fire = cls._date(row["next_fire_at"])
        if (status == ReminderStatus.ACTIVE) != (next_fire is not None) or row["version"] < 1 or (status in (ReminderStatus.FIRED, ReminderStatus.MISSED) and rule["kind"] not in ("at", "before_deadline", "before_event", "before_date")):
            raise ValueError("数据库中的提醒状态不一致")
        ZoneInfo(row["timezone"])
        return Reminder(row["id"], row["task_id"], rule, row["timezone"], status, row["reason"], next_fire, row["version"], cls._date(row["created_at"]), cls._date(row["updated_at"]))

    def _row_to_notification(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Notification:
        members = []
        now = datetime.now(UTC)
        for member in connection.execute("SELECT * FROM notification_members WHERE notification_id=? ORDER BY task_id", (row["id"],)).fetchall():
            task_row = connection.execute("SELECT * FROM personal_tasks WHERE id=?", (member["task_id"],)).fetchone()
            task = self._row_to_task(task_row)
            members.append(NotificationMember(member["task_id"], member["title_snapshot"], bool(member["missed"]), bool(member["overdue"]), task.status.value, is_overdue(task, now, self._timezone)))
        deliveries = tuple(NotificationDelivery(d["notification_id"], d["channel"], d["claim_id"], d["status"], self._date(d["attempted_at"]), self._date(d["reported_at"]), d["error"]) for d in connection.execute("SELECT * FROM notification_deliveries WHERE notification_id=?", (row["id"],)).fetchall())
        body = row["body"]
        if row["kind"] == "recovery_summary":
            missed_count = sum(member.missed for member in members)
            overdue_count = sum(member.overdue for member in members)
            body = f"离线期间有 {missed_count} 件事务错过提醒，目前有 {overdue_count} 件事务已逾期"
        return Notification(row["id"], row["kind"], row["occurrence_id"], self._date(row["created_at"]), self._date(row["read_at"]), row["title"], body, tuple(members), deliveries)

    @staticmethod
    def _encode(task: PersonalTask) -> tuple:
        deadline_kind = task.deadline.kind if task.deadline else None
        deadline_value = None
        if task.deadline:
            deadline_value = task.deadline.value.isoformat() if deadline_kind == "date" else task.deadline.value.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return (task.id, task.title, task.description, task.status.value, deadline_kind, deadline_value,
                task.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                task.updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                task.completed_at.isoformat(timespec="microseconds").replace("+00:00", "Z") if task.completed_at else None,
                task.version, json.dumps({"kind": task.planning.kind.value,
                    "event_time": SqlitePersonalTaskRepository._encode_deadline(task.planning.event_time),
                    "scheduled_time": SqlitePersonalTaskRepository._encode_deadline(task.planning.scheduled_time),
                    "time_text": task.planning.time_text, "related_task_id": task.planning.related_task_id}, ensure_ascii=False))

    @staticmethod
    def _encode_deadline(value: TaskDeadline | None) -> dict | None:
        return None if value is None else {"kind": value.kind, "value": value.value.isoformat()}

    @staticmethod
    def _decode_planning(value: str) -> TaskPlanning:
        data = json.loads(value)
        if not isinstance(data, dict) or set(data) != {"kind", "event_time", "scheduled_time", "time_text", "related_task_id"}:
            raise ValueError("数据库中的事务安排结构无效")
        def decode_deadline(item):
            if item is None:
                return None
            if not isinstance(item, dict) or set(item) != {"kind", "value"}:
                raise ValueError("数据库中的安排时间结构无效")
            return TaskDeadline(item["kind"], date.fromisoformat(item["value"]) if item["kind"] == "date" else datetime.fromisoformat(item["value"].replace("Z", "+00:00")))
        if not isinstance(data["time_text"], str) or (data["related_task_id"] is not None and not isinstance(data["related_task_id"], str)):
            raise ValueError("数据库中的事务安排字段无效")
        return TaskPlanning(PersonalTaskKind(data["kind"]), decode_deadline(data["event_time"]), decode_deadline(data["scheduled_time"]), data["time_text"], data["related_task_id"])

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> PersonalTask:
        kind, value = row["deadline_kind"], row["deadline_value"]
        deadline = None if kind is None else TaskDeadline(kind, date.fromisoformat(value) if kind == "date" else datetime.fromisoformat(value.replace("Z", "+00:00")))
        return PersonalTask(row["id"], row["title"], row["description"], PersonalTaskStatus(row["status"]),
                            deadline, datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(UTC),
                            datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).astimezone(UTC),
                            datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00")).astimezone(UTC) if row["completed_at"] else None,
                            row["version"], SqlitePersonalTaskRepository._decode_planning(row["planning_json"]))

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                existing_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' LIMIT 1"
                ).fetchone()
                if existing_table is not None:
                    raise PersonalTaskStorageError("版本 0 数据库已有用户表")
                connection.execute("""CREATE TABLE personal_tasks (
                    id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed','cancelled')),
                    deadline_kind TEXT CHECK(deadline_kind IN ('date','datetime') OR deadline_kind IS NULL),
                    deadline_value TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT, version INTEGER NOT NULL CHECK(version >= 1),
                    CHECK((deadline_kind IS NULL) = (deadline_value IS NULL)),
                    CHECK((status = 'completed') = (completed_at IS NOT NULL)))""")
                version = 1
            if version == 1:
                connection.execute("SELECT id,title,description,status,deadline_kind,deadline_value,created_at,updated_at,completed_at,version FROM personal_tasks LIMIT 0")
                schema = """
                    CREATE TABLE task_reminders(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES personal_tasks(id) ON DELETE CASCADE,rule_json TEXT NOT NULL,timezone TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','paused','cancelled','fired','missed')),reason TEXT,next_fire_at TEXT,version INTEGER NOT NULL CHECK(version>=1),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,CHECK((status='active')=(next_fire_at IS NOT NULL)));
                    CREATE TABLE reminder_occurrences(id TEXT PRIMARY KEY,reminder_id TEXT NOT NULL REFERENCES task_reminders(id) ON DELETE CASCADE,task_id TEXT NOT NULL REFERENCES personal_tasks(id) ON DELETE CASCADE,reminder_version INTEGER NOT NULL,scheduled_at TEXT NOT NULL,processed_at TEXT NOT NULL,outcome TEXT NOT NULL CHECK(outcome IN ('fired','missed')),missed_until TEXT,UNIQUE(reminder_id,reminder_version,scheduled_at));
                    CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL CHECK(kind IN ('reminder','recovery_summary')),occurrence_id TEXT UNIQUE REFERENCES reminder_occurrences(id) ON DELETE CASCADE,created_at TEXT NOT NULL,read_at TEXT,title TEXT NOT NULL,body TEXT NOT NULL,CHECK((kind='reminder')=(occurrence_id IS NOT NULL)));
                    CREATE TABLE notification_members(notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,task_id TEXT NOT NULL REFERENCES personal_tasks(id) ON DELETE CASCADE,title_snapshot TEXT NOT NULL,missed INTEGER NOT NULL CHECK(missed IN (0,1)),overdue INTEGER NOT NULL CHECK(overdue IN (0,1)),PRIMARY KEY(notification_id,task_id));
                    CREATE TABLE notification_deliveries(notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,channel TEXT NOT NULL,claim_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL CHECK(status IN ('attempted','accepted','failed')),attempted_at TEXT NOT NULL,reported_at TEXT,error TEXT,PRIMARY KEY(notification_id,channel));
                    CREATE TABLE reminder_scan_state(id INTEGER PRIMARY KEY CHECK(id=1),last_scan_at TEXT NOT NULL,timezone TEXT NOT NULL);
                    CREATE INDEX idx_reminders_due ON task_reminders(status,next_fire_at); CREATE INDEX idx_reminders_task ON task_reminders(task_id); CREATE INDEX idx_occurrences_task ON reminder_occurrences(task_id,processed_at); CREATE INDEX idx_members_task ON notification_members(task_id); CREATE INDEX idx_notifications_read ON notifications(read_at,id);
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                for query in (
                    "SELECT id,task_id,rule_json,timezone,status,reason,next_fire_at,version,created_at,updated_at FROM task_reminders LIMIT 0",
                    "SELECT id,reminder_id,task_id,reminder_version,scheduled_at,processed_at,outcome,missed_until FROM reminder_occurrences LIMIT 0",
                    "SELECT id,kind,occurrence_id,created_at,read_at,title,body FROM notifications LIMIT 0",
                    "SELECT notification_id,task_id,title_snapshot,missed,overdue FROM notification_members LIMIT 0",
                    "SELECT notification_id,channel,claim_id,status,attempted_at,reported_at,error FROM notification_deliveries LIMIT 0",
                    "SELECT id,last_scan_at,timezone FROM reminder_scan_state LIMIT 0",
                ):
                    connection.execute(query)
                from ..assistant.sqlite_repository import migrate_assistant_schema
                migrate_assistant_schema(connection)
                connection.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                for query in (
                    "SELECT id,title,description,status,deadline_kind,deadline_value,created_at,updated_at,completed_at,version FROM personal_tasks LIMIT 0",
                    "SELECT id,task_id,rule_json,timezone,status,reason,next_fire_at,version,created_at,updated_at FROM task_reminders LIMIT 0",
                    "SELECT id,reminder_id,task_id,reminder_version,scheduled_at,processed_at,outcome,missed_until FROM reminder_occurrences LIMIT 0",
                    "SELECT id,kind,occurrence_id,created_at,read_at,title,body FROM notifications LIMIT 0",
                    "SELECT notification_id,task_id,title_snapshot,missed,overdue FROM notification_members LIMIT 0",
                    "SELECT notification_id,channel,claim_id,status,attempted_at,reported_at,error FROM notification_deliveries LIMIT 0",
                    "SELECT id,last_scan_at,timezone FROM reminder_scan_state LIMIT 0",
                ):
                    connection.execute(query)
                from ..assistant.sqlite_repository import validate_assistant_schema
                validate_assistant_schema(connection)
                connection.execute("ALTER TABLE personal_tasks ADD COLUMN planning_json TEXT NOT NULL DEFAULT '{\"kind\":\"task\",\"event_time\":null,\"scheduled_time\":null,\"time_text\":\"\",\"related_task_id\":null}'")
                connection.execute("PRAGMA user_version=4")
                version = 4
            if version == 4:
                connection.execute("SELECT id,title,description,status,deadline_kind,deadline_value,created_at,updated_at,completed_at,version,planning_json FROM personal_tasks LIMIT 0")
                from ..assistant.sqlite_repository import validate_assistant_schema
                validate_assistant_schema(connection)
            else:
                raise PersonalTaskStorageError(f"不支持的数据库版本：{version}")
            connection.execute("COMMIT")
        except PersonalTaskStorageError:
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise PersonalTaskStorageError("初始化事务数据库失败") from error
        finally:
            # 关闭连接会撤销仍未提交的事务，包括版本检查拒绝的初始化。
            connection.close()

    async def create(self, task: PersonalTask) -> PersonalTask:
        return await asyncio.to_thread(self._create_sync, task)

    def _create_sync(self, task: PersonalTask) -> PersonalTask:
        with self._connection_scope(write=True) as connection:
            try:
                connection.execute("INSERT INTO personal_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", self._encode(task))
                return task
            except sqlite3.Error as error:
                raise PersonalTaskStorageError("创建事务失败") from error

    async def create_with_reminders(self, task: PersonalTask, reminders: tuple[Reminder, ...]) -> PersonalTask:
        return await asyncio.to_thread(self._create_with_reminders_sync, task, reminders)

    def _create_with_reminders_sync(self, task: PersonalTask, reminders: tuple[Reminder, ...]) -> PersonalTask:
        with self._connection_scope(write=True) as connection:
            try:
                connection.execute("INSERT INTO personal_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", self._encode(task))
                for reminder in reminders:
                    connection.execute(
                        "INSERT INTO task_reminders VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (reminder.id, reminder.task_id, json.dumps(reminder.rule, ensure_ascii=False), reminder.timezone,
                         reminder.status.value, reminder.reason, self._stamp(reminder.next_fire_at), reminder.version,
                         self._stamp(reminder.created_at), self._stamp(reminder.updated_at)),
                    )
                return task
            except sqlite3.Error as error:
                raise PersonalTaskStorageError("创建事务及初始提醒失败") from error

    async def get(self, task_id: str) -> PersonalTask | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> PersonalTask | None:
        with self._connection_scope() as connection:
            try:
                row = connection.execute("SELECT * FROM personal_tasks WHERE id=?", (task_id,)).fetchone()
                return self._row_to_task(row) if row else None
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise PersonalTaskStorageError("读取事务失败") from error

    async def get_detail(self, task_id: str) -> tuple[PersonalTask | None, tuple[Reminder, ...]]:
        return await asyncio.to_thread(self._get_detail_sync, task_id)

    def _get_detail_sync(self, task_id: str) -> tuple[PersonalTask | None, tuple[Reminder, ...]]:
        with self._connection_scope(snapshot=True) as connection:
            try:
                row = connection.execute("SELECT * FROM personal_tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    return None, ()
                reminders = tuple(self._row_to_reminder(item) for item in connection.execute(
                    "SELECT * FROM task_reminders WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall())
                return self._row_to_task(row), reminders
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise PersonalTaskStorageError("读取事务详情失败") from error

    async def list(self, statuses: tuple[PersonalTaskStatus, ...] | None) -> tuple[PersonalTask, ...]:
        return await asyncio.to_thread(self._list_sync, statuses)

    def _list_sync(self, statuses: tuple[PersonalTaskStatus, ...] | None) -> tuple[PersonalTask, ...]:
        connection = self._connect()
        try:
            if statuses is None:
                rows = connection.execute("SELECT * FROM personal_tasks").fetchall()
            else:
                marks = ",".join("?" for _ in statuses)
                rows = connection.execute(f"SELECT * FROM personal_tasks WHERE status IN ({marks})", tuple(s.value for s in statuses)).fetchall()
            return tuple(self._row_to_task(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise PersonalTaskStorageError("列出事务失败") from error
        finally:
            connection.close()

    async def read_task_snapshot(self) -> tuple[tuple[PersonalTask, ...], dict[str, ReminderSummary]]:
        return await asyncio.to_thread(self._read_task_snapshot_sync)

    def _read_task_snapshot_sync(self) -> tuple[tuple[PersonalTask, ...], dict[str, ReminderSummary]]:
        with self._connection_scope(snapshot=True) as connection:
            try:
                tasks = tuple(self._row_to_task(row) for row in connection.execute("SELECT * FROM personal_tasks").fetchall())
                rows = connection.execute("SELECT task_id,count(*) AS active_count,min(next_fire_at) AS next_fire_at FROM task_reminders WHERE status='active' GROUP BY task_id").fetchall()
                summaries = {row["task_id"]: ReminderSummary(row["active_count"], self._date(row["next_fire_at"])) for row in rows}
                for task in tasks:
                    summaries.setdefault(task.id, ReminderSummary(0, None))
                return tasks, summaries
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise PersonalTaskStorageError("查询事务失败") from error

    async def replace(self, task: PersonalTask, *, expected_version: int, now: datetime | None = None, timezone: ZoneInfo | None = None) -> PersonalTask:
        saved, _ = await self.replace_with_effects(task, expected_version=expected_version, now=now, timezone=timezone)
        return saved

    async def replace_with_effects(self, task: PersonalTask, *, expected_version: int, now: datetime | None = None, timezone: ZoneInfo | None = None) -> tuple[PersonalTask, tuple[ReminderChange, ...]]:
        return await asyncio.to_thread(self._replace_sync, task, expected_version, now or task.updated_at, timezone)

    def _replace_sync(self, task: PersonalTask, expected_version: int, now: datetime, timezone: ZoneInfo | None) -> tuple[PersonalTask, tuple[ReminderChange, ...]]:
        with self._connection_scope(write=True) as connection:
            changes: list[ReminderChange] = []
            try:
                old_row = connection.execute("SELECT * FROM personal_tasks WHERE id=?", (task.id,)).fetchone()
                old_task = self._row_to_task(old_row) if old_row else None
                values = self._encode(task)
                cursor = connection.execute("UPDATE personal_tasks SET title=?,description=?,status=?,deadline_kind=?,deadline_value=?,updated_at=?,completed_at=?,version=?,planning_json=? WHERE id=? AND version=?",
                (values[1], values[2], values[3], values[4], values[5], values[7], values[8], values[9], values[10], task.id, expected_version))
                if cursor.rowcount == 0:
                    exists = connection.execute("SELECT 1 FROM personal_tasks WHERE id=?", (task.id,)).fetchone()
                    raise PersonalTaskVersionConflict(task.id) if exists else PersonalTaskNotFound(task.id)
                if task.status not in (PersonalTaskStatus.PENDING, PersonalTaskStatus.IN_PROGRESS):
                    rows = connection.execute("SELECT id,reason,next_fire_at,version FROM task_reminders WHERE task_id=? AND status IN ('active','paused')", (task.id,)).fetchall()
                    connection.execute("UPDATE task_reminders SET status='cancelled',reason='task_ended',next_fire_at=NULL,version=version+1,updated_at=? WHERE task_id=? AND status IN ('active','paused')", (self._stamp(now), task.id))
                    changes.extend(ReminderChange(row["id"], "cancelled", "task_ended", None, row["version"] + 1) for row in rows)
                elif old_task and (old_task.deadline != task.deadline or old_task.planning.event_time != task.planning.event_time):
                    reminders = connection.execute("SELECT * FROM task_reminders WHERE task_id=? AND status IN ('active','paused')", (task.id,)).fetchall()
                    for row in reminders:
                        reminder = self._row_to_reminder(row)
                        if reminder.rule["kind"] not in ("before_deadline", "before_date", "before_event"):
                            continue
                        if reminder.rule["kind"] == "before_event" and old_task.planning.event_time == task.planning.event_time:
                            continue
                        if reminder.rule["kind"] != "before_event" and old_task.deadline == task.deadline:
                            continue
                        if reminder.rule["kind"] == "before_event":
                            event_time = task.planning.event_time
                            invalid_time = task.planning.kind != PersonalTaskKind.EVENT or event_time is None or event_time.kind != "datetime"
                            cancel_reason = "event_time_removed_or_changed_kind"
                        else:
                            event_time = None
                            invalid_time = task.deadline is None or task.deadline.kind != ("datetime" if reminder.rule["kind"] == "before_deadline" else "date")
                            cancel_reason = "deadline_removed_or_changed_kind"
                        if invalid_time:
                            connection.execute("UPDATE task_reminders SET status='cancelled',reason=?,next_fire_at=NULL,version=version+1,updated_at=? WHERE id=?", (cancel_reason, self._stamp(now), reminder.id))
                            changes.append(ReminderChange(reminder.id, "cancelled", cancel_reason, None, reminder.version + 1))
                        elif reminder.status == ReminderStatus.ACTIVE or (reminder.rule["kind"] == "before_event" and reminder.status == ReminderStatus.PAUSED and reminder.reason == "event_moved_past"):
                            adjusted = reschedule_relative(reminder, task, now)
                            connection.execute("UPDATE task_reminders SET status=?,reason=?,next_fire_at=?,version=?,updated_at=? WHERE id=?", (adjusted.status.value, adjusted.reason, self._stamp(adjusted.next_fire_at), adjusted.version, self._stamp(now), reminder.id))
                            action = "paused" if adjusted.status == ReminderStatus.PAUSED else "rescheduled"
                            changes.append(ReminderChange(reminder.id, action, adjusted.reason, adjusted.next_fire_at, adjusted.version))
                return task, tuple(changes)
            except (PersonalTaskNotFound, PersonalTaskVersionConflict):
                raise
            except sqlite3.Error as error:
                raise PersonalTaskStorageError("更新事务失败") from error

    async def delete(self, task_id: str, *, expected_version: int) -> None:
        await asyncio.to_thread(self._delete_sync, task_id, expected_version)

    def _delete_sync(self, task_id: str, expected_version: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM personal_tasks WHERE id=? AND version=?", (task_id, expected_version))
            if cursor.rowcount == 0:
                exists = connection.execute("SELECT 1 FROM personal_tasks WHERE id=?", (task_id,)).fetchone()
                raise PersonalTaskVersionConflict(task_id) if exists else PersonalTaskNotFound(task_id)
            summaries = connection.execute("SELECT n.id FROM notifications n WHERE n.kind='recovery_summary' AND NOT EXISTS(SELECT 1 FROM notification_members m WHERE m.notification_id=n.id)").fetchall()
            for summary in summaries:
                connection.execute("DELETE FROM notifications WHERE id=?", (summary[0],))
            for summary in connection.execute("SELECT id FROM notifications WHERE kind='recovery_summary'").fetchall():
                totals = connection.execute("SELECT count(*),sum(missed),sum(overdue) FROM notification_members WHERE notification_id=?", (summary[0],)).fetchone()
                connection.execute("UPDATE notifications SET body=? WHERE id=?", (f"离线期间有 {totals[1] or 0} 件事务错过提醒，目前有 {totals[2] or 0} 件事务已逾期", summary[0]))
            connection.execute("COMMIT")
        except (PersonalTaskNotFound, PersonalTaskVersionConflict):
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise PersonalTaskStorageError("删除事务失败") from error
        finally:
            connection.close()

    async def list_reminders(self, task_id: str) -> tuple[Reminder, ...]:
        return await asyncio.to_thread(self._list_reminders_sync, task_id)

    def _list_reminders_sync(self, task_id: str) -> tuple[Reminder, ...]:
        with self._connection_scope() as connection:
            try:
                return tuple(self._row_to_reminder(row) for row in connection.execute("SELECT * FROM task_reminders WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall())
            except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
                raise PersonalTaskStorageError("读取提醒失败") from error

    async def get_reminder(self, task_id: str, reminder_id: str) -> Reminder:
        return await asyncio.to_thread(self._get_reminder_sync, task_id, reminder_id)

    def _get_reminder_sync(self, task_id: str, reminder_id: str) -> Reminder:
        with self._connection_scope() as connection:
            try:
                row = connection.execute("SELECT * FROM task_reminders WHERE task_id=? AND id=?", (task_id, reminder_id)).fetchone()
                if row is None:
                    raise ReminderNotFound(reminder_id)
                return self._row_to_reminder(row)
            except ReminderNotFound:
                raise
            except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
                raise PersonalTaskStorageError("读取提醒失败") from error

    async def create_reminder(self, reminder: Reminder, *, expected_task_version: int) -> Reminder:
        return await asyncio.to_thread(self._create_reminder_sync, reminder, expected_task_version)

    def _create_reminder_sync(self, reminder: Reminder, expected_task_version: int) -> Reminder:
        with self._connection_scope(write=True) as connection:
            try:
                task = connection.execute("SELECT status,version FROM personal_tasks WHERE id=?", (reminder.task_id,)).fetchone()
                if task is None:
                    raise PersonalTaskNotFound(reminder.task_id)
                if task["version"] != expected_task_version:
                    raise PersonalTaskVersionConflict(reminder.task_id)
                if task["status"] not in ("pending", "in_progress"):
                    raise ValueError("已结束事务不能新增提醒")
                connection.execute("INSERT INTO task_reminders VALUES(?,?,?,?,?,?,?,?,?,?)", (reminder.id, reminder.task_id, json.dumps(reminder.rule, ensure_ascii=False), reminder.timezone, reminder.status.value, reminder.reason, self._stamp(reminder.next_fire_at), reminder.version, self._stamp(reminder.created_at), self._stamp(reminder.updated_at)))
                return reminder
            except (PersonalTaskNotFound, PersonalTaskVersionConflict):
                raise
            except (sqlite3.Error, ValueError) as error:
                raise PersonalTaskStorageError("创建提醒失败") from error

    async def replace_reminder(self, reminder: Reminder, *, expected_version: int, expected_task_version: int) -> Reminder:
        return await asyncio.to_thread(self._replace_reminder_sync, reminder, expected_version, expected_task_version)

    def _replace_reminder_sync(self, reminder: Reminder, expected_version: int, expected_task_version: int) -> Reminder:
        with self._connection_scope(write=True) as connection:
            try:
                task = connection.execute("SELECT status,version FROM personal_tasks WHERE id=?", (reminder.task_id,)).fetchone()
                if task is None:
                    raise PersonalTaskNotFound(reminder.task_id)
                if task["version"] != expected_task_version:
                    raise PersonalTaskVersionConflict(reminder.task_id)
                if reminder.status == ReminderStatus.ACTIVE and task["status"] not in ("pending", "in_progress"):
                    raise ValueError("已结束事务不能启用提醒")
                cursor = connection.execute("UPDATE task_reminders SET rule_json=?,timezone=?,status=?,reason=?,next_fire_at=?,version=?,updated_at=? WHERE task_id=? AND id=? AND version=?", (json.dumps(reminder.rule, ensure_ascii=False), reminder.timezone, reminder.status.value, reminder.reason, self._stamp(reminder.next_fire_at), reminder.version, self._stamp(reminder.updated_at), reminder.task_id, reminder.id, expected_version))
                if cursor.rowcount == 0:
                    exists = connection.execute("SELECT 1 FROM task_reminders WHERE id=? AND task_id=?", (reminder.id, reminder.task_id)).fetchone()
                    raise ReminderVersionConflict(reminder.id) if exists else ReminderNotFound(reminder.id)
                return reminder
            except (PersonalTaskNotFound, PersonalTaskVersionConflict, ReminderNotFound, ReminderVersionConflict):
                raise
            except (sqlite3.Error, ValueError) as error:
                raise PersonalTaskStorageError("更新提醒失败") from error

    async def process_due(self, *, now: datetime, timezone: ZoneInfo, startup: bool) -> None:
        await asyncio.to_thread(self._process_due_sync, now, timezone, startup)

    def _process_due_sync(self, now: datetime, timezone: ZoneInfo, startup: bool) -> None:
        connection = self._connect()
        now = now.astimezone(UTC)
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM reminder_scan_state WHERE id=1").fetchone()
            previous = self._date(state["last_scan_at"]) if state else None
            if previous and now < previous:
                connection.execute("COMMIT")
                return
            recovering = startup or previous is None or (now - previous).total_seconds() > 30
            due = connection.execute("SELECT r.*,t.title,t.status,t.deadline_kind,t.deadline_value FROM task_reminders r JOIN personal_tasks t ON t.id=r.task_id WHERE r.status='active' AND t.status IN ('pending','in_progress') AND r.next_fire_at<=? ORDER BY r.next_fire_at,r.id", (self._stamp(now),)).fetchall()
            missed_tasks: dict[str, tuple[str, bool]] = {}
            for row in due:
                reminder = self._row_to_reminder(row)
                scheduled = reminder.next_fire_at
                outcome = "missed" if recovering else "fired"
                occurrence_id = str(uuid4())
                connection.execute("INSERT INTO reminder_occurrences VALUES(?,?,?,?,?,?,?,?)", (occurrence_id, reminder.id, reminder.task_id, reminder.version, self._stamp(scheduled), self._stamp(now), outcome, self._stamp(now) if recovering else None))
                task = self._row_to_task(connection.execute("SELECT * FROM personal_tasks WHERE id=?", (reminder.task_id,)).fetchone())
                one_shot = reminder.rule["kind"] in ("at", "before_deadline", "before_date")
                following = None if one_shot else next_fire_at(reminder.rule, task, ZoneInfo(reminder.timezone), now)
                new_status = (ReminderStatus.MISSED if recovering else ReminderStatus.FIRED) if one_shot else ReminderStatus.ACTIVE
                connection.execute("UPDATE task_reminders SET status=?,next_fire_at=?,version=version+1,updated_at=? WHERE id=?", (new_status.value, self._stamp(following), self._stamp(now), reminder.id))
                if recovering:
                    missed_tasks[task.id] = (task.title, True)
                else:
                    deadline_text = f"；事务截止：{task.deadline.value}" if task.deadline else ""
                    cursor = connection.execute("INSERT INTO notifications(kind,occurrence_id,created_at,title,body) VALUES('reminder',?,?,?,?)", (occurrence_id, self._stamp(now), task.title, f"提醒时间：{scheduled.isoformat()}{deadline_text}"))
                    connection.execute("INSERT INTO notification_members VALUES(?,?,?,?,?)", (cursor.lastrowid, task.id, task.title, 0, 0))
            if recovering:
                if previous:
                    old_zone = ZoneInfo(state["timezone"])
                    for task_row in connection.execute("SELECT * FROM personal_tasks WHERE status IN ('pending','in_progress')").fetchall():
                        task = self._row_to_task(task_row)
                        if not is_overdue(task, now, timezone) or is_overdue(task, previous, old_zone):
                            continue
                        missed_tasks.setdefault(task.id, (task.title, False))
                if missed_tasks:
                    current_overdue = []
                    for task_row in connection.execute("SELECT * FROM personal_tasks WHERE status IN ('pending','in_progress')").fetchall():
                        task = self._row_to_task(task_row)
                        if is_overdue(task, now, timezone):
                            current_overdue.append(task)
                    for task in current_overdue:
                        missed_tasks.setdefault(task.id, (task.title, False))
                    missed_count = sum(missed for _, missed in missed_tasks.values())
                    cursor = connection.execute("INSERT INTO notifications(kind,created_at,title,body) VALUES('recovery_summary',?,?,?)", (self._stamp(now), "离线期间提醒汇总", f"离线期间有 {missed_count} 件事务错过提醒，目前有 {len(current_overdue)} 件事务已逾期"))
                    overdue_ids = {task.id for task in current_overdue}
                    for task_id, (title, missed) in missed_tasks.items():
                        connection.execute("INSERT INTO notification_members VALUES(?,?,?,?,?)", (cursor.lastrowid, task_id, title, int(missed), int(task_id in overdue_ids)))
            connection.execute("INSERT INTO reminder_scan_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET last_scan_at=excluded.last_scan_at,timezone=excluded.timezone", (self._stamp(now), str(timezone)))
            connection.execute("COMMIT")
        except (sqlite3.Error, ValueError, TypeError) as error:
            connection.rollback()
            raise PersonalTaskStorageError("扫描提醒失败") from error
        finally:
            connection.close()

    async def get_scan_time(self) -> datetime | None:
        return await asyncio.to_thread(self._get_scan_time_sync)

    def _get_scan_time_sync(self) -> datetime | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT last_scan_at FROM reminder_scan_state WHERE id=1").fetchone()
            return self._date(row[0]) if row else None
        finally:
            connection.close()

    async def list_notifications(self, *, unread_only: bool, before_id: int | None, limit: int) -> NotificationPage:
        return await asyncio.to_thread(self._list_notifications_sync, unread_only, before_id, limit)

    def _list_notifications_sync(self, unread_only: bool, before_id: int | None, limit: int) -> NotificationPage:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            clauses, params = [], []
            if unread_only: clauses.append("read_at IS NULL")
            if before_id is not None: clauses.append("id<?"); params.append(before_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(f"SELECT * FROM notifications{where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
            unread = connection.execute("SELECT count(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]
            watermark = connection.execute("SELECT coalesce(seq,0) FROM sqlite_sequence WHERE name='notifications'").fetchone()
            items = tuple(self._row_to_notification(connection, row) for row in rows)
            connection.execute("COMMIT")
            return NotificationPage(items, items[-1].id if len(items) == limit else None, unread, watermark[0] if watermark else 0)
        finally:
            connection.close()

    async def get_notification(self, notification_id: int) -> Notification:
        return await asyncio.to_thread(self._get_notification_sync, notification_id)

    def _get_notification_sync(self, notification_id: int) -> Notification:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotificationNotFound(notification_id)
            result = self._row_to_notification(connection, row)
            connection.execute("COMMIT")
            return result
        finally: connection.close()

    async def notification_changes(self, *, after_id: int, limit: int) -> NotificationChanges:
        return await asyncio.to_thread(self._notification_changes_sync, after_id, limit)

    def _notification_changes_sync(self, after_id: int, limit: int) -> NotificationChanges:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT * FROM notifications WHERE id>? ORDER BY id ASC LIMIT ?", (after_id, limit + 1)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            watermark_row = connection.execute("SELECT coalesce(seq,0) FROM sqlite_sequence WHERE name='notifications'").fetchone()
            watermark = watermark_row[0] if watermark_row else 0
            unread = connection.execute("SELECT count(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]
            items = tuple(self._row_to_notification(connection, row) for row in rows)
            next_id = items[-1].id if has_more and items else watermark
            connection.execute("COMMIT")
            return NotificationChanges(items, next_id, has_more, unread, watermark)
        finally: connection.close()

    async def mark_notification_read(self, notification_id: int, *, now: datetime) -> Notification:
        return await asyncio.to_thread(self._mark_notification_read_sync, notification_id, now)

    def _mark_notification_read_sync(self, notification_id: int, now: datetime) -> Notification:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE notifications SET read_at=coalesce(read_at,?) WHERE id=?", (self._stamp(now), notification_id))
            row = connection.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if row is None: raise NotificationNotFound(notification_id)
            connection.execute("COMMIT")
            return self._row_to_notification(connection, row)
        except Exception:
            connection.rollback()
            raise
        finally: connection.close()

    async def claim_notification_delivery(self, notification_id: int, *, channel: str, claim_id: str, now: datetime) -> NotificationDelivery | None:
        return await asyncio.to_thread(self._claim_notification_delivery_sync, notification_id, channel, claim_id, now)

    def _claim_notification_delivery_sync(self, notification_id: int, channel: str, claim_id: str, now: datetime) -> NotificationDelivery | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM notifications WHERE id=?", (notification_id,)).fetchone(): raise NotificationNotFound(notification_id)
            try:
                connection.execute("INSERT INTO notification_deliveries VALUES(?,?,?,'attempted',?,NULL,NULL)", (notification_id, channel, claim_id, self._stamp(now)))
            except sqlite3.IntegrityError:
                connection.execute("COMMIT"); return None
            connection.execute("COMMIT")
            return NotificationDelivery(notification_id, channel, claim_id, "attempted", now, None, None)
        except Exception:
            connection.rollback(); raise
        finally: connection.close()

    async def report_notification_delivery(self, notification_id: int, *, claim_id: str, status: str, error: str | None, now: datetime) -> NotificationDelivery:
        return await asyncio.to_thread(self._report_notification_delivery_sync, notification_id, claim_id, status, error, now)

    def _report_notification_delivery_sync(self, notification_id: int, claim_id: str, status: str, error: str | None, now: datetime) -> NotificationDelivery:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM notification_deliveries WHERE notification_id=? AND claim_id=?", (notification_id, claim_id)).fetchone()
            if row is None:
                if not connection.execute("SELECT 1 FROM notifications WHERE id=?", (notification_id,)).fetchone(): raise NotificationNotFound(notification_id)
                raise NotificationDeliveryConflict(claim_id)
            if row["status"] != "attempted" and (row["status"] != status or row["error"] != error): raise NotificationDeliveryConflict(claim_id)
            if row["status"] != "attempted":
                connection.execute("COMMIT")
                return NotificationDelivery(notification_id, row["channel"], claim_id, row["status"], self._date(row["attempted_at"]), self._date(row["reported_at"]), row["error"])
            connection.execute("UPDATE notification_deliveries SET status=?,reported_at=?,error=? WHERE notification_id=? AND claim_id=?", (status, self._stamp(now), error, notification_id, claim_id))
            updated = connection.execute("SELECT * FROM notification_deliveries WHERE notification_id=? AND claim_id=?", (notification_id, claim_id)).fetchone()
            connection.execute("COMMIT")
            return NotificationDelivery(notification_id, updated["channel"], claim_id, updated["status"], self._date(updated["attempted_at"]), self._date(updated["reported_at"]), updated["error"])
        except Exception:
            connection.rollback(); raise
        finally: connection.close()
