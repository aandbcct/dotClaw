"""纯快照、双游标和通知竞态的观察契约。"""

import asyncio
from dataclasses import replace

import pytest

from dotclaw.runtime.application.run_watch_service import RunWatchService, RunWatchCursor
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier
from dotclaw.runtime.adapters import RunRepositoryAdapter, InMemoryRunRepository
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.context import SuccessCommitFaultPoint
from dotclaw.runtime.domain.context import SuccessCommitIntent
from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome
from dotclaw.runtime.domain.facts import ApprovalRecord, ApprovalStatus, RunErrorCode
from dotclaw.bootstrap.session_interaction import SessionInteractionService
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.session.session import SessionManager
from tests.runtime_v2.test_delegation_lifecycle import _stack, _node
from tests.runtime_v2.test_e5_success_commit_recovery import _prepare_pending_success, FaultInjector, SuccessCommitInterrupted
from tests.runtime_v2.test_cancellation_persistence import _run, _record, _intent, NOW
from dotclaw.channel.web.app import _observation_activity
from dotclaw.channel.web.schemas import SSEEvent


@pytest.mark.parametrize("value", ["", "e-1-c0", "e0-c-1", "e1.0-c0", "e01-c0", "e0-c0\n", "e0-c0-extra", "e" + "1" * 110 + "-c0"])
def test_cursor_rejects_malformed_and_unbounded_inputs(value):
    with pytest.raises(ValueError):
        RunWatchCursor.parse(value)


@pytest.mark.parametrize("value", [True, -1, 0.5, "1"])
def test_cursor_is_strict_integer(value):
    with pytest.raises(ValueError):
        RunWatchCursor(value, 0)


def test_cursor_roundtrip_and_sse_header_injection_rejected():
    assert RunWatchCursor.parse("e12-c9") == RunWatchCursor(12, 9)
    assert str(RunWatchCursor(12, 9)) == "e12-c9"
    with pytest.raises(ValueError):
        SSEEvent("run.snapshot", {}, "e0-c0\nevent: forged")


@pytest.mark.parametrize("memory", [False, True])
async def test_snapshot_is_pure_and_strict_about_ownership(tmp_path, monkeypatch, memory):
    repository = InMemoryRunRepository() if memory else RunRepositoryAdapter(tmp_path)
    await repository.create_run(_run())
    async def forbidden(*args):
        raise AssertionError("观察不得触发补偿")
    monkeypatch.setattr(repository, "load_run", forbidden)
    watcher = RunWatchService(repository, CancellationService(), RunChangeNotifier(), "generation-one")
    assert (await watcher.snapshot("session", "run")).generation == "generation-one"
    with pytest.raises(PermissionError, match="run_session_mismatch"):
        await watcher.snapshot("another-session", "run")
    with pytest.raises(FileNotFoundError):
        await watcher.snapshot("session", "absent")
    with pytest.raises(ValueError):
        await watcher.snapshot("../session", "run")


@pytest.mark.parametrize("memory", [False, True])
async def test_active_occupancy_includes_pending_success_and_releases_complete_terminal(tmp_path, memory):
    repository = InMemoryRunRepository() if memory else RunRepositoryAdapter(tmp_path)
    completed = replace(_run(), state=AgentRunState(Ended(RunOutcome.COMPLETED)), ended_at=NOW)
    await repository.create_run(completed)
    assert await repository.list_active_runs("session") == ()

    intent = SuccessCommitIntent("conversation", None, RunOutcome.COMPLETED, "run", "session")
    pending = replace(completed, success_commit_intent=intent)
    await repository.save_run(pending)
    assert await repository.list_active_runs("session") == (pending,)
    assert (await repository.load_run_snapshot("session", "run")).state.is_ended()
    await repository.save_run(completed)
    assert await repository.list_active_runs("session") == ()


async def test_facade_does_not_hide_corrupt_storage_as_missing_target(tmp_path):
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    await repository.create_run(_run())
    service = SessionInteractionService(SessionManager(tmp_path), AgentRegistry(), coordinator, run_repository=repository)
    (tmp_path / "session" / "agent_runs" / "run" / "run.json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        await service.get_run("session", "run")
    with pytest.raises(ValueError):
        await service.cancel_session("session", "停止", expected_run_id="run")


async def test_watch_version_before_snapshot_does_not_lose_a_wakeup(tmp_path, monkeypatch):
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    await engine.prepare_run(_node("watched"))
    watcher = RunWatchService(repository, engine._cancellation_service, coordinator._notifier, "one")
    original = watcher.snapshot
    changed = False
    async def snapshot(session_id, run_id):
        nonlocal changed
        value = await original(session_id, run_id)
        if not changed:
            changed = True
            await repository.append_event(session_id, RunEvent(run_id, 1, RunEventType.RUN_STARTED, NOW))
            coordinator._notifier.notify(run_id)
        return value
    monkeypatch.setattr(watcher, "snapshot", snapshot)
    stream = watcher.watch("session-watched", "watched")
    assert (await anext(stream)).cursor.event_sequence == 0
    assert (await asyncio.wait_for(anext(stream), 1)).cursor.event_sequence == 1
    await stream.aclose()
    assert not coordinator._notifier._waiters


async def test_slow_consumer_never_holds_execution_and_terminal_closes_stream(tmp_path):
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    await engine.prepare_run(_node("watched"))
    watcher = RunWatchService(repository, engine._cancellation_service, coordinator._notifier, "one")
    stream = watcher.watch("session-watched", "watched")
    assert not (await anext(stream)).terminal
    await asyncio.wait_for(coordinator.start_prepared("watched"), 2)
    assert (await asyncio.wait_for(anext(stream), 1)).terminal
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_heartbeat_rechecks_and_reconnect_always_yields_full_snapshot(tmp_path, monkeypatch):
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    await engine.prepare_run(_node("watched"))
    watcher = RunWatchService(repository, engine._cancellation_service, coordinator._notifier, "one")
    async def heartbeat(run_id, version, timeout):
        assert timeout == 15
        return version
    monkeypatch.setattr(coordinator._notifier, "wait", heartbeat)
    stream = watcher.watch("session-watched", "watched", RunWatchCursor())
    assert (await anext(stream)).cursor == RunWatchCursor()
    assert (await anext(stream)).cursor == RunWatchCursor()
    await stream.aclose()
    with pytest.raises(ValueError, match="超出"):
        await anext(watcher.watch("session-watched", "watched", RunWatchCursor(100, 0)))


async def test_success_intent_masks_terminal_without_recovery_or_fact_mutation(tmp_path):
    injector = FaultInjector(SuccessCommitFaultPoint.AFTER_RUN_FINALIZATION)
    repository, _, _, completed, message, event, intent = await _prepare_pending_success(tmp_path, injector)
    with pytest.raises(SuccessCommitInterrupted):
        await repository.commit_success(completed, message, event, intent)
    watcher = RunWatchService(repository, CancellationService(), RunChangeNotifier(), "one")
    before = (tmp_path / completed.session_id / "agent_runs" / completed.run_id / "run.json").read_bytes()
    observation = await watcher.snapshot(completed.session_id, completed.run_id)
    assert observation.run.state.is_ended() and observation.committing and not observation.terminal
    assert _observation_activity(observation).status == "running"
    assert observation.cursor.event_sequence == 0
    assert (tmp_path / completed.session_id / "agent_runs" / completed.run_id / "run.json").read_bytes() == before


async def test_cancellation_intent_masks_settled_summary_until_intent_deleted(tmp_path, monkeypatch):
    repository = RunRepositoryAdapter(tmp_path)
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    original = repository._delete_cancellation_intent_sync
    def fail(*args):
        raise OSError("删除意图前中断")
    monkeypatch.setattr(repository, "_delete_cancellation_intent_sync", fail)
    with pytest.raises(OSError):
        await repository.commit_cancellation(_intent(run, record))
    watcher = RunWatchService(repository, CancellationService(), RunChangeNotifier(), "one")
    observation = await watcher.snapshot("session", "run")
    assert observation.run.state.is_ended() and not observation.terminal
    activity = _observation_activity(observation)
    assert activity.status == "cancelling" and activity.event_sequence == 1
    monkeypatch.setattr(repository, "_delete_cancellation_intent_sync", original)
    await repository.recover_cancellation_commit("session", "run")
    assert (await watcher.snapshot("session", "run")).terminal


@pytest.mark.parametrize("point", [SuccessCommitFaultPoint.BEFORE_SESSION_PROJECTION, SuccessCommitFaultPoint.AFTER_RUN_FINALIZATION])
@pytest.mark.parametrize("entry", ["resume", "start", "abandon", "approval", "delegation"])
async def test_reserved_success_blocks_all_business_recovery_without_effects_or_events(tmp_path, point, entry):
    """真实成功提交故障后，控制入口只返回保存中，不写事实、不消费审批、不重发能力。"""
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    repository._success_commit_fault_port = FaultInjector(point)
    calls = 0
    original = engine._llm_port.complete
    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)
    engine._llm_port.complete = complete
    request = _node("reserved")
    with pytest.raises(SuccessCommitInterrupted):
        await coordinator.submit(request)
    run = await repository.load_run_snapshot(request.session_id, request.run_id)
    if entry == "approval":
        await engine._approval_service._repository.create(ApprovalRecord("pending-approval", run.run_id, run.session_id, ApprovalStatus.PENDING, NOW))
    if entry == "delegation":
        await repository.create_run(replace(_run(), run_id="completed-child", parent_run_id=run.run_id,
            state=AgentRunState(Ended(RunOutcome.COMPLETED)), ended_at=NOW))
    path = tmp_path / request.session_id / "agent_runs" / request.run_id / "run.json"
    before = path.read_bytes()
    events = await repository.load_events(run.session_id, run.run_id)
    if entry == "resume":
        result = await coordinator.resume_run(run.run_id)
    elif entry == "start":
        result = await coordinator.start_prepared(run.run_id)
    elif entry == "abandon":
        result = await coordinator.abandon_run(run.run_id)
    elif entry == "approval":
        result = await coordinator.resolve_approval("pending-approval", True)
        assert await engine._approval_service.find_pending("pending-approval") is not None
    else:
        result = await coordinator.resume_delegation("completed-child")
    assert result.error.code is RunErrorCode.PERSISTENCE_FAILURE and not result.state.is_ended()
    assert calls == 1
    assert path.read_bytes() == before
    assert await repository.load_events(run.session_id, run.run_id) == events
    assert await repository.has_pending_success_commit(run.session_id, run.run_id)
