"""真实回环HTTP验证观察断流、重连和取消回执，不访问外部模型。"""

import asyncio
import json
import time
import logging
from dataclasses import replace

import pytest

from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier
from dotclaw.runtime.application.dto import RunResult
from dotclaw.runtime.domain.context import SuccessCommitFaultPoint
from dotclaw.runtime.domain.state import RunOutcome
from tests.runtime_v2.test_runtime_engine import _engine, _request, BlockedLLM
from tests.channel.web.test_sse_contract import _EngineInteraction, _FakeHost, _FakeSessionManager, _live_http_client
from tests.runtime_v2.test_e5_success_commit_recovery import _prepare_pending_success, FaultInjector, SuccessCommitInterrupted
from tests.runtime_v2.test_cancellation_persistence import _run, _record, _intent


def _http_host(root, model=None):
    notifier = RunChangeNotifier()
    engine = _engine(root, run_change_notifier=notifier)
    if model:
        engine._llm_port = model
    service = _EngineInteraction(engine, root, run_change_notifier=notifier)
    host = _FakeHost(_FakeSessionManager(("session-live", "other")), service)
    return host, service


async def _event(lines):
    block = []
    async with asyncio.timeout(3):
        async for line in lines:
            if not line and block:
                break
            block.append(line)
    return next(line[4:].strip() for line in block if line.startswith("id:")), json.loads(next(line[5:].strip() for line in block if line.startswith("data:")))


async def test_real_http_watch_disconnect_reconnect_and_cancellation_waits_for_cleanup(tmp_path, record_property):
    model = BlockedLLM()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    original = model.complete
    async def complete(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()
    model.complete = complete
    host, service = _http_host(tmp_path, model)
    committed: list[float] = []
    commit = service.engine._run_repository.commit_cancellation
    async def timed_commit(intent):
        await commit(intent)
        committed.append(time.perf_counter())
    service.engine._run_repository.commit_cancellation = timed_commit
    work = asyncio.create_task(service.submit("session-live", "测试"))
    await model.started.wait()
    run = await service.engine.active_run("session-live")
    path = f"/api/v1/sessions/session-live/runs/{run.run_id}/events"
    try:
        async with _live_http_client(host, model.release) as client:
            async with client.stream("GET", path) as response:
                assert response.status_code == 200
                cursor, first = await _event(response.aiter_lines())
                assert first["status"] == "running" and first["generation"]
                assert first["steps"][-1]["kind"] == "model"
            assert not work.done() and not model.cancel_observed.is_set()
            async with client.stream("GET", path, headers={"Last-Event-ID": cursor}) as response:
                lines = response.aiter_lines()
                repeated, refreshed = await _event(lines)
                assert repeated == cursor and refreshed == first
                receipt = await client.post("/api/v1/sessions/session-live/cancel", json={"run_id": run.run_id})
                duplicate = await client.post("/api/v1/sessions/session-live/cancel", json={"run_id": run.run_id, "reason": "重复"})
                assert receipt.status_code == duplicate.status_code == 202
                assert receipt.json()["request_id"] == duplicate.json()["request_id"]
                await cleanup_entered.wait()
                activity = (await client.get(f"/api/v1/sessions/session-live/activity?run_id={run.run_id}")).json()
                assert activity["status"] == "cancelling" and not work.done()
                cleanup_release.set()
                snapshots = [refreshed]
                async with asyncio.timeout(4):
                    while snapshots[-1]["status"] != "cancelled":
                        _, value = await _event(lines)
                        snapshots.append(value)
                    assert [line async for line in lines] == []
                assert all(value["generation"] == first["generation"] for value in snapshots)
                assert snapshots[-1]["cancellation"]["phase"] == "settled"
                assert snapshots[-1]["control_revision"] >= receipt.json()["control_revision"]
                latency = (time.perf_counter() - committed[-1]) * 1000
                record_property("committed_to_observed_ms", latency)
                assert latency < 500
            assert (await work).state.outcome() is RunOutcome.CANCELLED
    finally:
        cleanup_release.set()
        model.release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize("cursor", ["bad", "e-1-c0", "e10000-c0", "e0-c10000"])
async def test_real_http_rejects_bad_or_future_cursor_before_stream_headers(tmp_path, cursor):
    model = BlockedLLM()
    host, service = _http_host(tmp_path, model)
    run_id = await service.coordinator.prepare_run(replace(_request("session-live"), run_id="prepared"))
    async with _live_http_client(host, model.release) as client:
        response = await client.get(f"/api/v1/sessions/session-live/runs/{run_id}/events", headers={"Last-Event-ID": cursor})
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_cursor"
    assert not model.started.is_set()


async def test_real_http_stale_target_never_cancels_new_run(tmp_path):
    host, service = _http_host(tmp_path)
    previous = await service.coordinator.submit(_request("session-live"))
    model = BlockedLLM()
    service.engine._llm_port = model
    work = asyncio.create_task(service.coordinator.submit(_request("session-live")))
    await model.started.wait()
    active = await service.engine.active_run("session-live")
    try:
        async with _live_http_client(host, model.release) as client:
            old = await client.post("/api/v1/sessions/session-live/cancel", json={"run_id": previous.run_id})
            assert old.status_code == 200 and old.json()["outcome"] == "completed"
            assert old.json()["disposition"] == "already_ended"
            assert not model.cancel_observed.is_set()
            assert await service.repository.load_cancellation("session-live", active.run_id) is None
            missing = await client.post("/api/v1/sessions/session-live/cancel", json={"run_id": "absent"})
            wrong = await client.post("/api/v1/sessions/other/cancel", json={"run_id": active.run_id})
            unspecified = await client.post("/api/v1/sessions/session-live/cancel", json={})
            assert missing.status_code == wrong.status_code == 404
            assert unspecified.status_code == 422
            wrong_stream = await client.get(f"/api/v1/sessions/other/runs/{active.run_id}/events")
            assert wrong_stream.status_code == 404
            wrong_activity = await client.get(f"/api/v1/sessions/other/activity?run_id={active.run_id}")
            assert wrong_activity.status_code == 404
            model.release.set()
            await work
    finally:
        model.release.set()
        await asyncio.gather(work, return_exceptions=True)


async def test_pending_success_is_not_recovered_or_announced_by_get_post_or_watch(tmp_path):
    injector = FaultInjector(SuccessCommitFaultPoint.AFTER_RUN_FINALIZATION)
    repository, _, _, completed, final, event, intent = await _prepare_pending_success(tmp_path, injector)
    with pytest.raises(SuccessCommitInterrupted):
        await repository.commit_success(completed, final, event, intent)
    host, service = _http_host(tmp_path)
    host.session_manager = _FakeSessionManager((completed.session_id,))
    # 控制入口若返回过早的终态，POST传输边界也必须按待提交事实屏蔽。
    async def premature(*args, **kwargs):
        return RunResult(completed.run_id, completed.state)
    service.submit = premature
    async with _live_http_client(host, asyncio.Event()) as client:
        activity = await client.get(f"/api/v1/sessions/{completed.session_id}/activity?run_id={completed.run_id}")
        assert activity.json()["status"] == "running"
        latest = await client.get(f"/api/v1/sessions/{completed.session_id}/activity")
        assert latest.json()["run_id"] == completed.run_id and latest.json()["status"] == "running"
        occupied = await service.coordinator.submit(_request(completed.session_id))
        assert occupied.error.code.value == "session_busy"
        cancel = await client.post(f"/api/v1/sessions/{completed.session_id}/cancel", json={"run_id": completed.run_id})
        assert cancel.status_code == 409 and cancel.json()["error"]["code"] == "committing_success"
        post = await client.post(f"/api/v1/sessions/{completed.session_id}/messages", json={"content": "触发过早结果"})
        assert "event: run.completed" not in post.text and "event: stream.error" in post.text
        async with client.stream("GET", f"/api/v1/sessions/{completed.session_id}/runs/{completed.run_id}/events") as response:
            _, snapshot = await _event(response.aiter_lines())
            assert snapshot["status"] == "running" and snapshot["event_sequence"] == 0
    assert await repository.has_pending_success_commit(completed.session_id, completed.run_id)
    injector.enabled = False
    await repository.recover_pending_success_commits()
    assert await service.engine.active_run(completed.session_id) is None
    assert (await service.coordinator.submit(_request(completed.session_id))).state.outcome() is RunOutcome.COMPLETED


async def test_pending_cancellation_retry_returns_202_and_replays_frozen_intent(tmp_path, monkeypatch):
    host, service = _http_host(tmp_path)
    host.session_manager = _FakeSessionManager(("session",))
    repository = service.engine._run_repository
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    original = repository._delete_cancellation_intent_sync
    def interrupted(*args):
        raise OSError("意图删除前中断")
    monkeypatch.setattr(repository, "_delete_cancellation_intent_sync", interrupted)
    with pytest.raises(OSError):
        await repository.commit_cancellation(_intent(run, record))
    assert (await repository.load_run_snapshot("session", "run")).state.is_ended()
    monkeypatch.setattr(repository, "_delete_cancellation_intent_sync", original)
    async with _live_http_client(host, asyncio.Event()) as client:
        before = await client.get("/api/v1/sessions/session/activity?run_id=run")
        assert before.json()["status"] == "cancelling"
        latest = await client.get("/api/v1/sessions/session/activity")
        assert latest.json()["run_id"] == "run" and latest.json()["status"] == "cancelling"
        occupied = await service.coordinator.submit(_request("session"))
        assert occupied.error.code.value == "session_busy"
        assert len(await repository.list_runs()) == 1
        response = await client.post("/api/v1/sessions/session/cancel", json={"run_id": "run", "reason": "不能覆盖首次原因"})
        assert response.status_code == 202
        assert response.json()["disposition"] == "already_requested"
        assert response.json()["request_id"] == record.request.request_id
        assert response.json()["outcome"] is None
        assert (await asyncio.wait_for(service.coordinator.wait_terminal("run"), 2)).state.outcome() is RunOutcome.CANCELLED
    persisted = await repository.load_cancellation("session", "run")
    assert persisted.request == record.request
    assert len(await repository.load_events("session", "run")) == 2
    assert await service.engine.active_run("session") is None
    assert (await service.coordinator.submit(_request("session"))).state.outcome() is RunOutcome.COMPLETED


async def test_pending_cancellation_without_record_is_explicit_inconsistency(tmp_path, monkeypatch):
    _, service = _http_host(tmp_path)
    repository = service.engine._run_repository
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    def interrupted(*args):
        raise OSError("保留意图")
    monkeypatch.setattr(repository, "_delete_cancellation_intent_sync", interrupted)
    with pytest.raises(OSError):
        await repository.commit_cancellation(_intent(run, record))
    (tmp_path / "session" / "agent_runs" / "run" / "cancellation.json").unlink()
    with pytest.raises(RuntimeError, match="持久化事实不一致"):
        await service.coordinator.cancel("run", "不允许伪造原请求")


@pytest.mark.parametrize("failure", ["corrupt", "permission"])
async def test_storage_errors_are_500_not_ownership_denials_and_logs_are_sanitized(tmp_path, monkeypatch, caplog, failure):
    host, service = _http_host(tmp_path)
    await service.coordinator.prepare_run(replace(_request("session-live"), run_id="stored"))
    if failure == "corrupt":
        (tmp_path / "session-live" / "agent_runs" / "stored" / "run.json").write_text("{PRIVATE_PAYLOAD", encoding="utf-8")
    else:
        def denied(*args):
            raise PermissionError(13, "PRIVATE_PERMISSION", "C:/PRIVATE_FILE/run.json")
        monkeypatch.setattr(service.repository, "_find_run_sync", denied)
    logger = logging.getLogger("dotclaw.channel.web")
    logger.addHandler(caplog.handler)
    try:
        async with _live_http_client(host, asyncio.Event()) as client:
            activity = await client.get("/api/v1/sessions/session-live/activity?run_id=stored")
            stream = await client.get("/api/v1/sessions/session-live/runs/stored/events")
            cancel = await client.post("/api/v1/sessions/session-live/cancel", json={"run_id": "stored"})
            assert activity.status_code == stream.status_code == cancel.status_code == 500
            assert all(response.json()["error"]["code"] == "internal_error" for response in (activity, stream, cancel))
            assert "PRIVATE" not in activity.text + stream.text + cancel.text + caplog.text
            assert "web.run.storage_failed" in caplog.text
    finally:
        logger.removeHandler(caplog.handler)
