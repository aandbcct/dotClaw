import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest

from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskStatus
from dotclaw.personal_tasks.repository import PersonalTaskStorageError, PersonalTaskVersionConflict
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository


def task(version=1):
    now = datetime(2026, 11, 18, tzinfo=UTC)
    return PersonalTask("t1", "事务", "", PersonalTaskStatus.PENDING, None, now, now, None, version)


@pytest.mark.asyncio
async def test_reopen_persists_and_compare_and_swap_allows_one_writer(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    first = SqlitePersonalTaskRepository(path)
    second = SqlitePersonalTaskRepository(path)
    await first.initialize()
    await first.create(task())
    reopened = SqlitePersonalTaskRepository(path)
    await reopened.initialize()
    assert await reopened.get("t1") == task()
    left = task(2)
    right = PersonalTask("t1", "另一个标题", "", PersonalTaskStatus.PENDING, None,
        left.created_at, left.updated_at, None, 2)
    outcomes = await asyncio.gather(first.replace(left, expected_version=1), second.replace(right, expected_version=1), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in outcomes) == 1
    assert sum(isinstance(result, PersonalTaskVersionConflict) for result in outcomes) == 1
    assert (await reopened.get("t1")).version == 2


def test_connect_maps_pragma_failure_and_closes_connection(tmp_path, monkeypatch):
    class Connection:
        closed = False
        row_factory = None

        def execute(self, statement):
            raise sqlite3.OperationalError("readonly")

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)
    repository = SqlitePersonalTaskRepository(tmp_path / "tasks.sqlite3")
    with pytest.raises(PersonalTaskStorageError) as captured:
        repository._connect()
    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
    assert connection.closed
