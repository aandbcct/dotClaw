"""用真实协调器和假模型验证严格放弃，不执行真实工具。"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from dotclaw.bootstrap.session_interaction import SessionInteractionService
from dotclaw.runtime.adapters import CheckpointRepositoryAdapter
from dotclaw.runtime.application.session_run_coordinator import RunNotRecoverableError, SessionRunCoordinator
from dotclaw.runtime.domain.state import AgentRunState, Suspended, SuspendReason, RunStage, RunOutcome
from tests.channel.web.test_api_contract import _client, _host, _session
from tests.runtime_v2.test_e4_runtime_safety import _engine, _request, FlakyLLM, ScriptedCounter, ScriptedCompactor


async def _fixture(tmp_path):
    engine, repository = _engine(tmp_path, ScriptedCounter(10), ScriptedCompactor(), FlakyLLM())
    coordinator = SessionRunCoordinator(engine)
    active = await coordinator.submit(_request(1))
    host, _ = _host(_session("session-e4"), _session("other"))
    host.session_interaction = SessionInteractionService(host.session_manager, host.agent_registry, coordinator, run_repository=repository)
    return host, coordinator, engine, repository, active.run_id


async def test_web_abandons_recoverable_keeps_record_and_accepts_next_request(tmp_path):
    host, coordinator, engine, repository, run_id = await _fixture(tmp_path)
    original = engine.abandon_run

    async def locked_abandon(target):
        assert await coordinator.is_session_executing("session-e4")
        return await original(target)

    engine.abandon_run = locked_abandon
    async with _client(host) as client:
        result = await client.post(f"/api/v1/sessions/session-e4/runs/{run_id}/abandon")
        repeated = await client.post(f"/api/v1/sessions/session-e4/runs/{run_id}/abandon")
    assert result.status_code == 200
    assert result.json()["status"] == "abandoned"
    assert result.json()["error"]["code"] == "cancelled"
    assert repeated.status_code == 409 and repeated.json()["error"]["code"] == "run_not_recoverable"
    assert (await repository.find_run(run_id)).state.outcome() is RunOutcome.ABANDONED
    assert await CheckpointRepositoryAdapter(tmp_path).load("session-e4", run_id) is None
    assert (await coordinator.submit(_request(1))).state.outcome() is RunOutcome.COMPLETED


@pytest.mark.parametrize("session,run", [("missing", "target"), ("other", "target"), ("session-e4", "missing")])
async def test_abandon_wrong_session_or_run_never_calls_engine(tmp_path, session, run):
    host, _, engine, _, run_id = await _fixture(tmp_path)
    engine.abandon_run = AsyncMock()
    async with _client(host) as client:
        result = await client.post(f"/api/v1/sessions/{session}/runs/{run_id if run == 'target' else run}/abandon")
    assert result.status_code == 404
    engine.abandon_run.assert_not_called()


@pytest.mark.parametrize("reason", [SuspendReason.APPROVAL, SuspendReason.DELEGATION])
async def test_waiting_controls_cannot_be_abandoned_from_web(tmp_path, reason):
    host, _, engine, repository, run_id = await _fixture(tmp_path)
    run = await repository.find_run(run_id)
    await repository.save_run(replace(run, state=AgentRunState(mode=Suspended(reason, "control", RunStage.CALLING_LLM))))
    engine.abandon_run = AsyncMock()
    async with _client(host) as client:
        result = await client.post(f"/api/v1/sessions/session-e4/runs/{run_id}/abandon")
    assert result.status_code == 409 and result.json()["error"]["code"] == "run_not_recoverable"
    engine.abandon_run.assert_not_called()


async def test_resume_in_progress_rejects_abandon_immediately_without_waiting(tmp_path):
    host, coordinator, engine, _, run_id = await _fixture(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    complete = engine._llm_port.complete

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await complete(*args, **kwargs)

    engine._llm_port.complete = blocked
    resumed = asyncio.create_task(coordinator.resume_run(run_id))
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with _client(host) as client:
            result = await asyncio.wait_for(client.post(f"/api/v1/sessions/session-e4/runs/{run_id}/abandon"), 1)
        assert result.status_code == 409 and result.json()["error"]["code"] == "session_busy"
        assert not release.is_set() and not resumed.done()
    finally:
        release.set()
        await resumed


async def test_abandon_validation_and_execution_share_lock_against_resume(tmp_path):
    _, coordinator, engine, _, run_id = await _fixture(tmp_path)
    checking, release = asyncio.Event(), asyncio.Event()
    active_run = engine.active_run

    async def blocked_check(session):
        assert await coordinator.is_session_executing(session)
        checking.set()
        await release.wait()
        return await active_run(session)

    engine.active_run = blocked_check
    abandoned = asyncio.create_task(coordinator.abandon_run(run_id, only_recoverable=True))
    resumed = None
    try:
        await asyncio.wait_for(checking.wait(), 2)
        resumed = asyncio.create_task(coordinator.resume_run(run_id))
        await asyncio.sleep(0)
        assert not resumed.done()
    finally:
        release.set()
        assert (await asyncio.wait_for(abandoned, 2)).state.outcome() is RunOutcome.ABANDONED
        if resumed:
            assert (await resumed).state.outcome() is RunOutcome.FAILED


async def test_strict_branch_rechecks_current_run_and_preserves_default_branch(tmp_path):
    _, coordinator, engine, repository, run_id = await _fixture(tmp_path)
    run = await repository.find_run(run_id)
    engine.active_run = AsyncMock(return_value=replace(run, run_id="other-run"))
    with pytest.raises(RunNotRecoverableError):
        await coordinator.abandon_run(run_id, only_recoverable=True)
    with pytest.raises(RunNotRecoverableError):
        await coordinator.abandon_run("missing", only_recoverable=True)
    # 默认入口仍沿用已有行为，不引入新的严格校验。
    assert (await coordinator.abandon_run(run_id)).state.outcome() is RunOutcome.ABANDONED
