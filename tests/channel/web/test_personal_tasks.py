from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dotclaw.channel.web.app import create_app
from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskStatus, PersonalTaskView
from dotclaw.personal_tasks.repository import PersonalTaskVersionConflict
from dotclaw.personal_tasks.service import PersonalTaskService
from dotclaw.personal_tasks.reminder_service import ReminderService
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
            assert listed.json()["items"][0]["reminder_summary"] == {"active_count": 0, "next_fire_at": None}
            assert listed.json()["total"] == 1 and listed.json()["offset"] == 0 and listed.json()["limit"] == 50
            assert listed.json()["has_more"] is False
            invalid_range = await client.get("/api/v1/personal-tasks", params={"deadline_from": "2026-11-22", "deadline_until": "2026-11-22"})
            assert invalid_range.status_code == 422
            detail = await client.get(url)
            assert detail.status_code == 200 and detail.json()["reminders"] == []
            edited = await client.put(url, json={"title": "已编辑", "description": "说明", "deadline": deadline, "expected_version": 1})
            assert edited.status_code == 200, edited.text
            assert edited.json()["reminder_changes"] == []
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
                assert changed.json()["reminder_changes"] == []
                version += 1
                assert changed.json()["version"] == version
                assert (changed.json()["completed_at"] is not None) == (state == "completed")
            deleted = await client.delete(url, params={"expected_version": version})
            assert deleted.status_code == 204
            assert (await client.get(url)).status_code == 404


@pytest.mark.asyncio
async def test_create_task_with_initial_reminder_is_atomic_and_keeps_task_response(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    repository = SqlitePersonalTaskRepository(tmp_path / "task-reminder-create.sqlite3")
    await repository.initialize()
    zone = ZoneInfo("UTC")
    host = SimpleNamespace(personal_task_service=PersonalTaskService(repository, timezone=zone, clock=lambda: now),
        reminder_service=ReminderService(repository, timezone=zone, clock=lambda: now),
        scheduler_enabled=True, _config=SimpleNamespace(personal_tasks=SimpleNamespace(timezone="UTC")),
        project_root=None, shutdown=AsyncMock())

    async def host_factory():
        return host

    app = create_app(host_factory=host_factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            invalid = await client.post("/api/v1/personal-tasks", json={
                "title": "原子验证",
                "reminders": [
                    {"kind": "at", "at": "2030-01-01T12:01:00Z"},
                    {"kind": "before_deadline", "offset_minutes": 60},
                ],
            })
            assert invalid.status_code == 422
            assert (await client.get("/api/v1/personal-tasks?filter=all")).json()["items"] == []

            created = await client.post("/api/v1/personal-tasks", json={
                "title": "含初始提醒",
                "reminders": [{"kind": "at", "at": "2030-01-01T12:01:00Z"}],
            })
            assert created.status_code == 201, created.text
            assert "reminders" not in created.json()
            detail = await client.get(f"/api/v1/personal-tasks/{created.json()['id']}")
            assert detail.status_code == 200 and len(detail.json()["reminders"]) == 1
            reminders = await client.get(f"/api/v1/personal-tasks/{created.json()['id']}/reminders")
            assert reminders.status_code == 200
            assert len(reminders.json()["items"]) == 1


@pytest.mark.asyncio
async def test_event_planning_http_roundtrip_old_put_and_plan_filters(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    repository = SqlitePersonalTaskRepository(tmp_path / "planning-http.sqlite3")
    await repository.initialize()
    host = SimpleNamespace(personal_task_service=PersonalTaskService(repository, timezone=ZoneInfo("UTC"), clock=lambda: now),
        project_root=None, shutdown=AsyncMock())

    async def host_factory():
        return host

    app = create_app(host_factory=host_factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            planning = {"kind": "event", "event_time": {"kind": "datetime", "value": "2030-01-01T14:00:00Z"},
                "scheduled_time": None, "time_text": "下午两点", "related_task_id": None}
            created = await client.post("/api/v1/personal-tasks", json={"title": "考试", "planning": planning})
            assert created.status_code == 201, created.text
            assert created.json()["planning"] == planning
            url = f"/api/v1/personal-tasks/{created.json()['id']}"
            detail = await client.get(url)
            assert detail.status_code == 200
            assert detail.json()["planning"] == planning
            edited = await client.put(url, json={"title": "考试已确认", "description": "", "deadline": None,
                "expected_version": 1})
            assert edited.status_code == 200, edited.text
            assert edited.json()["planning"] == planning
            scheduled = await client.get("/api/v1/personal-tasks", params={"plan_filter": "scheduled"})
            unscheduled = await client.get("/api/v1/personal-tasks", params={"plan_filter": "unscheduled"})
            assert scheduled.status_code == 200 and [item["id"] for item in scheduled.json()["items"]] == [created.json()["id"]]
            assert unscheduled.status_code == 200 and unscheduled.json()["items"] == []
            assert (await client.get("/api/v1/personal-tasks", params={"plan_filter": "unknown"})).status_code == 422


@pytest.mark.asyncio
async def test_status_response_contains_committed_reminder_effects(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    repository = SqlitePersonalTaskRepository(tmp_path / "task-effects.sqlite3")
    await repository.initialize()
    zone = ZoneInfo("UTC")
    host = SimpleNamespace(personal_task_service=PersonalTaskService(repository, timezone=zone, clock=lambda: now),
        reminder_service=ReminderService(repository, timezone=zone, clock=lambda: now), scheduler_enabled=True,
        _config=SimpleNamespace(personal_tasks=SimpleNamespace(timezone="UTC")), project_root=None, shutdown=AsyncMock())

    async def host_factory():
        return host

    app = create_app(host_factory=host_factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/personal-tasks", json={"title": "事务", "reminders": [
                {"kind": "daily", "local_time": "09:00"}]})
            assert created.status_code == 201, created.text
            reminder_list = await client.get(f"/api/v1/personal-tasks/{created.json()['id']}/reminders")
            reminder_id = reminder_list.json()["items"][0]["id"]
            changed = await client.post(f"/api/v1/personal-tasks/{created.json()['id']}/status",
                json={"status": "completed", "expected_version": 1})
            assert changed.status_code == 200, changed.text
            assert changed.json()["reminder_changes"] == [{"reminder_id": reminder_id, "action": "cancelled",
                "reason": "task_ended", "next_fire_at": None, "version": 2}]
