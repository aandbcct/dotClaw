import sqlite3

import pytest

from dotclaw.personal_tasks.repository import PersonalTaskStorageError
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from dotclaw.personal_tasks.service import PersonalTaskService
from zoneinfo import ZoneInfo


@pytest.mark.asyncio
async def test_fresh_database_migrates_zero_to_three_and_creates_assistant_tables(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"personal_tasks", "task_reminders", "assistant_requests", "assistant_operations", "assistant_result_sets"} <= tables


@pytest.mark.asyncio
async def test_v2_to_v3_migration_preserves_existing_task_data(tmp_path):
    path = tmp_path / "v2.sqlite3"
    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()
    created = await PersonalTaskService(repository, timezone=ZoneInfo("UTC")).create(title="旧数据保留")

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE assistant_requests")
        connection.execute("DROP TABLE assistant_operations")
        connection.execute("DROP TABLE assistant_result_sets")
        connection.execute("PRAGMA user_version=2")

    await repository.initialize()
    persisted = await repository.get(created.task.id)
    assert persisted.title == "旧数据保留"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_unknown_database_version_is_rejected_without_rewriting_version(tmp_path):
    path = tmp_path / "future.sqlite3"
    path.touch()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=9")

    with pytest.raises(PersonalTaskStorageError, match="不支持的数据库版本"):
        await SqlitePersonalTaskRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
