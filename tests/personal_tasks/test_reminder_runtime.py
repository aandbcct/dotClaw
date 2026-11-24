import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest
from zoneinfo import ZoneInfo

from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from dotclaw.scheduler.reminder_scheduler import ReminderScheduler


@pytest.mark.asyncio
async def test_version_one_database_migrates_without_losing_tasks(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE personal_tasks (
        id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed','cancelled')),
        deadline_kind TEXT CHECK(deadline_kind IN ('date','datetime') OR deadline_kind IS NULL),
        deadline_value TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        completed_at TEXT, version INTEGER NOT NULL CHECK(version >= 1),
        CHECK((deadline_kind IS NULL) = (deadline_value IS NULL)),
        CHECK((status = 'completed') = (completed_at IS NOT NULL)))""")
    connection.execute("INSERT INTO personal_tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (
        "legacy-task", "迁移保留", "说明", "pending", "date", "2030-01-02",
        "2030-01-01T00:00:00.000000Z", "2030-01-01T00:00:00.000000Z", None, 4,
    ))
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()
    task = await repository.get("legacy-task")
    connection = sqlite3.connect(path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()

    assert version == 4
    assert task is not None
    assert task.title == "迁移保留"
    assert task.version == 4
    assert task.deadline.value.isoformat() == "2030-01-02"
    assert task.planning.kind.value == "task"
    assert task.planning.event_time is None
    assert {"task_reminders", "reminder_occurrences", "notifications", "notification_members", "notification_deliveries", "reminder_scan_state",
            "assistant_requests", "assistant_operations", "assistant_result_sets"} <= tables


@pytest.mark.asyncio
async def test_version_three_migration_preserves_assistant_requests_and_receipts(tmp_path):
    path = tmp_path / "v3.sqlite3"
    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE personal_tasks DROP COLUMN planning_json")
        connection.execute("INSERT INTO assistant_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "request-1", "session-1", "create_task", "保存中的请求", "awaiting_input", "{}", "[]",
            None, None, None, "[]", 1, "2030-01-01T00:00:00Z", "2030-01-01T00:00:00Z"))
        connection.execute("INSERT INTO assistant_operations VALUES(?,?,?,?,?,?,?,?,?)", (
            "operation-1", "session-1", "run-1", "call-1", "tasks.create", "hash-1", "request-1",
            "{\"ok\":true}", "2030-01-01T00:00:00Z"))
        connection.execute("PRAGMA user_version=3")

    await repository.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT id,summary,payload_json FROM assistant_requests").fetchone() == (
            "request-1", "保存中的请求", "{}")
        assert connection.execute("SELECT operation_key,result_json FROM assistant_operations").fetchone() == (
            "operation-1", "{\"ok\":true}")


@pytest.mark.asyncio
async def test_reminder_scheduler_runs_initial_recovery_and_periodic_tick():
    class Service:
        def __init__(self):
            self.calls: list[bool] = []
            self.ticked = asyncio.Event()

        async def scan(self, *, startup: bool = False) -> None:
            self.calls.append(startup)
            if not startup:
                self.ticked.set()

    service = Service()
    scheduler = ReminderScheduler(service)
    await scheduler.start()
    await asyncio.wait_for(service.ticked.wait(), timeout=4)
    await scheduler.stop()

    assert service.calls[0] is True
    assert service.calls[1] is False
