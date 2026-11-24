from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from dotclaw.assistant.schemas import BatchPrepareInput, TaskCreateInput, TaskQueryInput
from dotclaw.channel.web.assistant_routes import router

from .test_service import add_user_run, make_context, make_service


@pytest.mark.asyncio
async def test_real_result_api_reads_saved_run_and_rejects_foreign_run(tmp_path):
    service, _, _ = await make_service(tmp_path / "assistant.sqlite3")
    await service.create_task(TaskCreateInput(title="API真实事务"), make_context())
    query_context = make_context(call_id="query", key="query-key")
    await service.query_tasks(TaskQueryInput(), query_context)

    app = FastAPI()
    app.include_router(router)
    app.state.host = SimpleNamespace(assistant_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/assistant/results", params={"run_id": "run-1"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["run_id"] == "run-1"
        assert len(payload["operations"]) == 2
        assert any(item["result"]["operation"] == "tasks.query" for item in payload["operations"])
        assert len(payload["result_sets"]) == 1

        foreign = await client.get("/api/v1/assistant/results", params={"run_id": "foreign-run"})
        assert foreign.status_code == 404
        assert foreign.json()["error"]["code"] == "run_not_found"

        invalid = await client.get("/api/v1/assistant/results", params={"run_id": ""})
        assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_proposal_api_returns_saved_preview_and_rejects_stale_version(tmp_path):
    service, _, run_repository = await make_service(tmp_path / "assistant.sqlite3")
    first = await service.create_task(TaskCreateInput(title="提案一"), make_context(key="create-1"))
    second = await service.create_task(TaskCreateInput(title="提案二"), make_context(call_id="create-2", key="create-2"))
    add_user_run(run_repository, run_id="proposal-run", message_id="proposal-message")
    prepared = await service.prepare_batch(
        BatchPrepareInput(summary="批量改名", items=[
            {"action": "update", "task_id": first.task_refs[0].id,
             "expected_version": first.task_refs[0].version, "patch": {"title": "新一"}},
            {"action": "update", "task_id": second.task_refs[0].id,
             "expected_version": second.task_refs[0].version, "patch": {"title": "新二"}},
        ]),
        make_context(run_id="proposal-run", call_id="prepare", key="proposal-prepare", message_id="proposal-message"),
    )
    request = prepared.data.request

    app = FastAPI()
    app.include_router(router)
    app.state.host = SimpleNamespace(assistant_service=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/assistant/requests/{request.id}/proposal",
            params={"version": request.version, "hash": request.proposal_hash},
        )
        assert response.status_code == 200
        assert [item["after"]["title"] for item in response.json()["items"]] == ["新一", "新二"]

        stale = await client.get(
            f"/api/v1/assistant/requests/{request.id}/proposal",
            params={"version": request.version + 1, "hash": request.proposal_hash},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "request_conflict"

        missing = await client.get(
            "/api/v1/assistant/requests/missing/proposal",
            params={"version": request.version, "hash": request.proposal_hash},
        )
        assert missing.status_code == 404
