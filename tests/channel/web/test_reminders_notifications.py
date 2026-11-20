from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from dotclaw.channel.web.app import create_app
from dotclaw.personal_tasks.notification_service import NotificationService
from dotclaw.personal_tasks.models import PersonalTaskStatus, TaskDeadline
from dotclaw.personal_tasks.reminder_service import ReminderService
from dotclaw.personal_tasks.service import PersonalTaskService
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@asynccontextmanager
async def _client(repository: SqlitePersonalTaskRepository, clock: Clock):
    zone = ZoneInfo("UTC")
    host = SimpleNamespace(
        personal_task_service=PersonalTaskService(repository, timezone=zone, clock=clock),
        reminder_service=ReminderService(repository, timezone=zone, clock=clock),
        notification_service=NotificationService(repository, clock=clock),
        scheduler_enabled=True,
        _config=SimpleNamespace(personal_tasks=SimpleNamespace(timezone="UTC")),
        project_root=None,
        shutdown=AsyncMock(),
    )

    async def host_factory():
        return host

    app = create_app(host_factory=host_factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_notification_http_roundtrip_with_real_sqlite(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    clock = Clock(now)
    repository = SqlitePersonalTaskRepository(tmp_path / "notifications.sqlite3", ZoneInfo("UTC"))
    await repository.initialize()

    async with _client(repository, clock) as client:
        created = await client.post("/api/v1/personal-tasks", json={"title": "提醒事务"})
        assert created.status_code == 201, created.text
        task = created.json()
        rule = {"kind": "at", "at": (now + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")}
        made = await client.post(f"/api/v1/personal-tasks/{task['id']}/reminders", json={"rule": rule, "expected_task_version": 1})
        assert made.status_code == 201, made.text

        reminder_list = await client.get(f"/api/v1/personal-tasks/{task['id']}/reminders")
        assert reminder_list.status_code == 200, reminder_list.text
        assert len(reminder_list.json()["items"]) == 1

        await repository.process_due(now=now, timezone=ZoneInfo("UTC"), startup=True)
        clock.value = now + timedelta(seconds=11)
        await repository.process_due(now=clock.value, timezone=ZoneInfo("UTC"), startup=False)

        listing = await client.get("/api/v1/notifications")
        assert listing.status_code == 200, listing.text
        notification = listing.json()["items"][0]
        assert notification["members"][0]["task_id"] == task["id"]
        notification_id = notification["id"]

        detail = await client.get(f"/api/v1/notifications/{notification_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["members"][0]["title_snapshot"] == "提醒事务"

        changes = await client.get("/api/v1/notifications/changes", params={"after_id": 0})
        assert changes.status_code == 200, changes.text
        assert [item["id"] for item in changes.json()["items"]] == [notification_id]

        read = await client.post(f"/api/v1/notifications/{notification_id}/read")
        assert read.status_code == 200, read.text
        assert read.json()["read_at"] is not None

        claim = await client.post(f"/api/v1/notifications/{notification_id}/deliveries/claim", json={"channel": "browser"})
        assert claim.status_code == 200, claim.text
        assert claim.json()["status"] == "attempted"
        report = await client.post(f"/api/v1/notifications/{notification_id}/deliveries/report", json={"claim_id": claim.json()["claim_id"], "status": "accepted"})
        assert report.status_code == 200, report.text
        assert report.json()["status"] == "accepted"

        final_detail = await client.get(f"/api/v1/notifications/{notification_id}")
        assert final_detail.status_code == 200, final_detail.text
        assert final_detail.json()["deliveries"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_http_create_all_seven_reminder_rules(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    clock = Clock(now)
    repository = SqlitePersonalTaskRepository(tmp_path / "rules.sqlite3", ZoneInfo("UTC"))
    await repository.initialize()

    async with _client(repository, clock) as client:
        general = (await client.post("/api/v1/personal-tasks", json={"title": "周期事务"})).json()
        datetime_task = (await client.post("/api/v1/personal-tasks", json={"title": "时刻截止", "deadline": {"kind": "datetime", "value": "2031-01-02T12:00:00Z"}})).json()
        date_task = (await client.post("/api/v1/personal-tasks", json={"title": "日期截止", "deadline": {"kind": "date", "value": "2031-01-02"}})).json()
        rules = [
            (general, {"kind": "at", "at": "2030-01-02T12:00:00Z"}),
            (datetime_task, {"kind": "before_deadline", "offset_minutes": 60}),
            (date_task, {"kind": "before_date", "days_before": 1, "local_time": "09:15"}),
            (general, {"kind": "daily", "local_time": "09:15"}),
            (general, {"kind": "weekly", "weekdays": [0, 2, 4], "local_time": "09:15"}),
            (general, {"kind": "monthly", "day": 31, "local_time": "09:15"}),
            (general, {"kind": "yearly", "month": 2, "day": 29, "local_time": "09:15"}),
        ]
        for task, rule in rules:
            response = await client.post(f"/api/v1/personal-tasks/{task['id']}/reminders", json={"rule": rule, "expected_task_version": task["version"]})
            assert response.status_code == 201, response.text

        listed = await client.get(f"/api/v1/personal-tasks/{general['id']}/reminders")
        assert listed.status_code == 200, listed.text
        assert {item["rule"]["kind"] for item in listed.json()["items"]} == {"at", "daily", "weekly", "monthly", "yearly"}


@pytest.mark.asyncio
async def test_deadline_changes_and_task_completion_update_reminders_atomically(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    clock = Clock(now)
    repository = SqlitePersonalTaskRepository(tmp_path / "links.sqlite3", ZoneInfo("UTC"))
    await repository.initialize()
    tasks = PersonalTaskService(repository, timezone=ZoneInfo("UTC"), clock=clock)
    reminders = ReminderService(repository, timezone=ZoneInfo("UTC"), clock=clock)

    datetime_task = await tasks.create(title="时刻截止事务", deadline=TaskDeadline("datetime", now + timedelta(hours=1)))
    relative = await reminders.create(datetime_task.task.id, rule={"kind": "before_deadline", "offset_minutes": 30}, expected_task_version=1)
    moved = await tasks.update(datetime_task.task.id, title="时刻截止事务", description="", deadline=TaskDeadline("datetime", now + timedelta(minutes=10)), expected_version=1)
    paused = await repository.get_reminder(datetime_task.task.id, relative.id)
    assert moved.task.version == 2
    assert paused.status.value == "paused"
    assert paused.reason == "deadline_moved_past"

    date_task = await tasks.create(title="日期截止事务", deadline=TaskDeadline("date", now.date() + timedelta(days=10)))
    date_reminder = await reminders.create(date_task.task.id, rule={"kind": "before_date", "days_before": 2, "local_time": "09:00"}, expected_task_version=1)
    await tasks.update(date_task.task.id, title="日期截止事务", description="", deadline=None, expected_version=1)
    cancelled = await repository.get_reminder(date_task.task.id, date_reminder.id)
    assert cancelled.status.value == "cancelled"
    assert cancelled.reason == "deadline_removed_or_changed_kind"

    recurring_task = await tasks.create(title="周期事务")
    recurring = await reminders.create(recurring_task.task.id, rule={"kind": "daily", "local_time": "09:00"}, expected_task_version=1)
    await tasks.change_status(recurring_task.task.id, status=PersonalTaskStatus.COMPLETED, expected_version=1)
    ended = await repository.get_reminder(recurring_task.task.id, recurring.id)
    assert ended.status.value == "cancelled"
    assert ended.reason == "task_ended"


@pytest.mark.asyncio
async def test_recovery_summary_union_counts_and_notification_pagination(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    previous = now - timedelta(minutes=1)
    clock = Clock(previous)
    zone = ZoneInfo("UTC")
    repository = SqlitePersonalTaskRepository(tmp_path / "recovery.sqlite3", zone)
    await repository.initialize()
    tasks = PersonalTaskService(repository, timezone=zone, clock=clock)
    reminders = ReminderService(repository, timezone=zone, clock=clock)

    missed_task = await tasks.create(title="错过提醒")
    await reminders.create(missed_task.task.id, rule={"kind": "at", "at": (previous + timedelta(seconds=30)).isoformat()}, expected_task_version=1)
    old_overdue_a = await tasks.create(title="已有逾期一", deadline=TaskDeadline("date", previous.date() - timedelta(days=1)))
    old_overdue_b = await tasks.create(title="已有逾期二", deadline=TaskDeadline("date", previous.date() - timedelta(days=1)))
    await repository.process_due(now=previous, timezone=zone, startup=True)
    clock.value = now
    await repository.process_due(now=now, timezone=zone, startup=False)

    page = await repository.list_notifications(unread_only=False, before_id=None, limit=1)
    assert len(page.items) == 1
    summary = await repository.get_notification(page.items[0].id)
    assert summary.kind == "recovery_summary"
    assert summary.body == "离线期间有 1 件事务错过提醒，目前有 2 件事务已逾期"
    assert {member.task_id for member in summary.members} == {missed_task.task.id, old_overdue_a.task.id, old_overdue_b.task.id}
    assert sum(member.missed for member in summary.members) == 1
    assert sum(member.overdue for member in summary.members) == 2

    for title in ("分页通知一", "分页通知二"):
        task = await tasks.create(title=title)
        await reminders.create(task.task.id, rule={"kind": "at", "at": (now + timedelta(seconds=10)).isoformat()}, expected_task_version=1)
    clock.value = now + timedelta(seconds=11)
    await repository.process_due(now=clock.value, timezone=zone, startup=False)

    async with _client(repository, clock) as client:
        first = await client.get("/api/v1/notifications", params={"limit": 1})
        assert first.status_code == 200, first.text
        assert first.json()["next_before_id"] is not None
        older = await client.get("/api/v1/notifications", params={"before_id": first.json()["next_before_id"], "limit": 1})
        assert older.status_code == 200, older.text
        changes = await client.get("/api/v1/notifications/changes", params={"after_id": 0, "limit": 1})
        assert changes.status_code == 200, changes.text
        if changes.json()["has_more"]:
            second = await client.get("/api/v1/notifications/changes", params={"after_id": changes.json()["next_after_id"], "limit": 1})
            assert second.status_code == 200, second.text
