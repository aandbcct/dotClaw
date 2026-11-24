import sqlite3

import pytest

from dotclaw.personal_tasks.repository import PersonalTaskStorageError
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from dotclaw.personal_tasks.service import PersonalTaskService
from zoneinfo import ZoneInfo
from dotclaw.assistant.schemas import RequestSaveInput, TaskCreateInput

from .test_service import make_context, make_service


@pytest.mark.asyncio
async def test_fresh_database_migrates_zero_to_four_and_creates_assistant_tables(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"personal_tasks", "task_reminders", "assistant_requests", "assistant_operations", "assistant_result_sets"} <= tables


@pytest.mark.asyncio
async def test_v2_to_v4_migration_preserves_existing_task_data(tmp_path):
    path = tmp_path / "v2.sqlite3"
    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()
    created = await PersonalTaskService(repository, timezone=ZoneInfo("UTC")).create(title="旧数据保留")

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE assistant_requests")
        connection.execute("DROP TABLE assistant_operations")
        connection.execute("DROP TABLE assistant_result_sets")
        connection.execute("ALTER TABLE personal_tasks DROP COLUMN planning_json")
        connection.execute("PRAGMA user_version=2")

    await repository.initialize()
    persisted = await repository.get(created.task.id)
    assert persisted.title == "旧数据保留"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


@pytest.mark.asyncio
async def test_v3_to_v4_migration_preserves_assistant_requests_and_operation_receipts(tmp_path):
    path = tmp_path / "v3.sqlite3"
    service, task_repository, _ = await make_service(path)
    create_input = TaskCreateInput(title="已有回执事务")
    create_context = make_context(call_id="legacy-create", key="legacy-create")
    created = await service.create_task(create_input, create_context)
    request_result = await service.save_request(
        RequestSaveInput(
            kind="create_task", summary="保留未结请求",
            payload={
                "intent_args": {"action": "create_task", "title": None},
                "missing_fields": ["title"], "candidate_task_ids": [], "selected_task_ids": [], "temporal_values": [],
            },
            source_refs=[],
        ),
        make_context(call_id="legacy-draft", key="legacy-draft"),
    )

    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE personal_tasks DROP COLUMN planning_json")
        connection.execute("PRAGMA user_version=3")

    await task_repository.initialize()
    replayed = await service.create_task(create_input, create_context)
    request = await service._repository.get_request("personal-assistant", request_result.data.request.id)
    assert replayed.model_dump(mode="json") == created.model_dump(mode="json")
    assert request is not None and request.summary == "保留未结请求"
    assert (await task_repository.get(created.task_refs[0].id)).planning.kind.value == "task"


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
