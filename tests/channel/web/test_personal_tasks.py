from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dotclaw.channel.web.app import create_app
from dotclaw.personal_tasks.models import PersonalTask, PersonalTaskStatus, PersonalTaskView
from dotclaw.personal_tasks.repository import PersonalTaskVersionConflict


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
