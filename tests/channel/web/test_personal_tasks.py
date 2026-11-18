from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dotclaw.channel.web.app import create_app
from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskStatus, PersonalTaskView
from dotclaw.personal_tasks.repository import PersonalTaskVersionConflict
from dotclaw.personal_tasks.service import PersonalTaskService
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from zoneinfo import ZoneInfo


@pytest.mark.asyncio
async def test_http_rejects_non_contract_dates_and_maps_version_conflict():
    task = PersonalTask("t1", "x", "", PersonalTaskStatus.PENDING, None, datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 18, tzinfo=UTC), None, 1)
    service = SimpleNamespace(create=AsyncMock(return_value=PersonalTaskView(task, False)), update=AsyncMock(side_effect=PersonalTaskVersionConflict("t1")))
    host = SimpleNamespace(personal_task_service=service, project_root=None, shutdown=AsyncMock())

    async def host_factory():
        return host

    app = create_app(host_factory=host_factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            invalid_date = await client.post("/api/v1/personal-tasks", json={"title": "x", "deadline": {"kind": "date", "value": "2026-W47-3"}})
            invalid_time = await client.post("/api/v1/personal-tasks", json={"title": "x", "deadline": {"kind": "datetime", "value": "2026-11-18 10:00:00+08:00"}})
            unknown_field = await client.post("/api/v1/personal-tasks", json={"title": "x", "id": "spoofed"})
            created = await client.post("/api/v1/personal-tasks", json={"title": "x"})
            conflict = await client.put(f"/api/v1/personal-tasks/{created.json()['id']}", json={"title": "y", "description": "", "deadline": None, "expected_version": 2})
    assert invalid_date.status_code == 422
    assert invalid_time.status_code == 422
    assert unknown_field.status_code == 422
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "personal_task_version_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [None, {"kind": "date", "value": "2026-11-20"},
    {"kind": "datetime", "value": "2026-11-20T15:00:00+08:00"}])
async def test_real_task_http_lifecycle_and_persistence(tmp_path, deadline):
    # 使用真实服务与临时数据库，覆盖请求到持久化再到响应的完整链路。
    path = tmp_path / "tasks.sqlite3"
    repository = SqlitePersonalTaskRepository(path)
    await repository.initialize()
    host = SimpleNamespace(personal_task_service=PersonalTaskService(repository, timezone=ZoneInfo("Asia/Shanghai")),
        project_root=None, shutdown=AsyncMock())

    async def host_factory():
        return host

    app = create_app(host_factory=host_factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/personal-tasks", json={"title": "测试事务", "deadline": deadline})
            assert created.status_code == 201, created.text
            task = created.json()
            expected_deadline = deadline
            if deadline is not None and deadline["kind"] == "datetime":
                expected_deadline = {"kind": "datetime", "value": "2026-11-20T07:00:00Z"}
            assert task["deadline"] == expected_deadline
            url = f"/api/v1/personal-tasks/{task['id']}"
            listed = await client.get("/api/v1/personal-tasks")
            assert listed.status_code == 200, listed.text
            assert listed.json()["items"][0]["id"] == task["id"]
            assert listed.json()["items"][0]["deadline"] == expected_deadline
            edited = await client.put(url, json={"title": "已编辑", "description": "说明", "deadline": deadline, "expected_version": 1})
            assert edited.status_code == 200, edited.text
            conflict = await client.put(url, json={"title": "旧版本", "description": "", "deadline": None, "expected_version": 1})
            assert conflict.status_code == 409
            # 重建仓储和服务，检查不依赖进程内缓存的数据可读性。
            reopened = SqlitePersonalTaskRepository(path)
            await reopened.initialize()
            host.personal_task_service = PersonalTaskService(reopened, timezone=ZoneInfo("Asia/Shanghai"))
            assert (await client.get(url)).json()["title"] == "已编辑"
            version = 2
            for state in ("in_progress", "completed", "pending", "cancelled"):
                changed = await client.post(url + "/status", json={"status": state, "expected_version": version})
                assert changed.status_code == 200, changed.text
                version += 1
                assert changed.json()["version"] == version
                assert (changed.json()["completed_at"] is not None) == (state == "completed")
            deleted = await client.delete(url, params={"expected_version": version})
            assert deleted.status_code == 204
            assert (await client.get(url)).status_code == 404
