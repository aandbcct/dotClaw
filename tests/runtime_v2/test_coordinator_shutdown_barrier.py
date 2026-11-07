"""关闭接收屏障覆盖已排队提交、准备和业务恢复。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from dotclaw.runtime.domain.state import RunOutcome
from tests.runtime_v2.test_delegation_lifecycle import _stack, _node
from tests.runtime_v2.test_delegation_port import ParentChildLLM, BlockingChildLLM
from tests.runtime_v2.test_runtime_engine import BlockedLLM, ApprovalLLM, ApprovalTool
from dotclaw.runtime.application.session_run_coordinator import SessionBusyError
from tests.runtime_v2.test_delegation_lifecycle import _prepared_child


async def _queue_on_held_session(coordinator, session_id, operation, monkeypatch):
    """真实持有Session锁，事件证明被测调用已进入取锁路径，不依赖调度sleep。"""
    original = coordinator._get_lock
    lock = await original(session_id)
    await lock.acquire()
    queued = asyncio.Event()
    async def get_lock(identifier):
        found = await original(identifier)
        if identifier == session_id:
            queued.set()
        return found
    monkeypatch.setattr(coordinator, "_get_lock", get_lock)
    task = asyncio.create_task(operation())
    await asyncio.wait_for(queued.wait(), 2)
    return lock, task


@pytest.mark.parametrize("entry", ["prepare", "submit", "submit_prepared"])
async def test_shutdown_rejects_queued_new_run_without_materializing_it(tmp_path, monkeypatch, entry):
    model = BlockedLLM()
    _, repository, coordinator, _, _ = _stack(tmp_path, model)
    request = _node("late")
    factory_called = False
    async def factory():
        nonlocal factory_called
        factory_called = True
        return request
    async def operation():
        if entry == "prepare":
            return await coordinator.prepare_run(request)
        if entry == "submit":
            return await coordinator.submit(request)
        return await coordinator.submit_prepared(request.session_id, factory)
    lock, task = await _queue_on_held_session(coordinator, request.session_id, operation, monkeypatch)
    try:
        await coordinator.shutdown(0.01)
    finally:
        lock.release()
    with pytest.raises(RuntimeError, match="停止"):
        await task
    assert await repository.find_run(request.run_id) is None
    assert not model.started.is_set() and not factory_called


@pytest.mark.parametrize("entry", ["resume_created", "approval", "delegation"])
async def test_shutdown_rejects_queued_business_recovery(tmp_path, monkeypatch, entry):
    model = ParentChildLLM() if entry == "delegation" else ApprovalLLM() if entry == "approval" else BlockedLLM()
    engine, repository, coordinator, adapter, _ = _stack(tmp_path, model)
    request = replace(_node("parent"), agent_id="parent-agent")
    if entry == "resume_created":
        await coordinator.prepare_run(request)
        method, arguments = "resume_run", (request.run_id,)
    elif entry == "approval":
        engine._tool_port = ApprovalTool()
        suspended = await coordinator.submit(request)
        assert suspended.approval_id
        method, arguments = "resolve_approval", (suspended.approval_id, True)
    else:
        suspended = await coordinator.submit(request)
        assert suspended.child_run_id
        await adapter.wait_terminal(suspended.child_run_id)
        method, arguments = "resume_delegation", (suspended.child_run_id,)
    called = False
    original = getattr(engine, method)
    async def guarded_engine_entry(*args, **kwargs):
        nonlocal called
        called = True
        return await original(*args, **kwargs)
    monkeypatch.setattr(engine, method, guarded_engine_entry)
    async def operation():
        return await getattr(coordinator, method)(*arguments)
    lock, task = await _queue_on_held_session(coordinator, request.session_id, operation, monkeypatch)
    try:
        await coordinator.shutdown(0.01)
    finally:
        lock.release()
    with pytest.raises(RuntimeError, match="停止"):
        await task
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    assert not called
    assert (await repository.find_run(request.run_id)).state.outcome() is RunOutcome.CANCELLED
    if entry == "resume_created":
        assert not model.started.is_set()


async def test_shutdown_rejects_scheduled_driver_that_was_waiting_for_session(tmp_path, monkeypatch):
    model = BlockedLLM()
    _, repository, coordinator, _, _ = _stack(tmp_path, model)
    request = _node("scheduled")
    await coordinator.prepare_run(request)
    async def operation():
        return await coordinator.start_prepared(request.run_id)
    lock, task = await _queue_on_held_session(coordinator, request.session_id, operation, monkeypatch)
    try:
        await coordinator.shutdown(0.01)
    finally:
        lock.release()
    with pytest.raises(RuntimeError, match="停止"):
        await task
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    assert not model.started.is_set()
    assert (await repository.find_run(request.run_id)).state.outcome() is RunOutcome.CANCELLED


async def test_shutdown_allows_waiting_for_existing_driver_but_refuses_new_start(tmp_path):
    model = BlockedLLM()
    _, _, coordinator, _, _ = _stack(tmp_path, model)
    request = _node("existing")
    await coordinator.prepare_run(request)
    entered, release = asyncio.Event(), asyncio.Event()
    original = model.complete
    async def complete(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        finally:
            entered.set()
            await release.wait()
    model.complete = complete
    coordinator.schedule_prepared(request.run_id)
    await model.started.wait()
    await coordinator.shutdown(0.01)
    assert entered.is_set()
    coordinator.schedule_prepared(request.run_id)
    observer = asyncio.create_task(coordinator.start_prepared(request.run_id))
    release.set()
    assert (await asyncio.wait_for(observer, 2)).state.outcome() is RunOutcome.CANCELLED
    assert (await coordinator.wait_terminal(request.run_id)).state.outcome() is RunOutcome.CANCELLED
    with pytest.raises(RuntimeError, match="停止"):
        coordinator.schedule_prepared("another-run")


@pytest.mark.parametrize("entry", ["engine", "coordinator"])
async def test_abandon_parent_with_live_child_is_rejected_without_releasing_occupancy(tmp_path, entry):
    model = BlockingChildLLM()
    engine, repository, coordinator, adapter, _ = _stack(tmp_path, model)
    request = replace(_node("parent"), agent_id="parent-agent")
    parent = await coordinator.submit(request)
    await asyncio.wait_for(model.child_started.wait(), 2)
    with pytest.raises(ValueError, match="先取消子树"):
        await (engine if entry == "engine" else coordinator).abandon_run(request.run_id)
    assert (await repository.find_run(request.run_id)).state.is_waiting_delegation()
    assert (await engine.active_run(request.session_id)).run_id == request.run_id
    assert not (await repository.find_run(parent.child_run_id)).state.is_ended()
    await coordinator.cancel(request.run_id, "放弃被拒绝后正常取消子树")
    assert (await asyncio.wait_for(coordinator.wait_terminal(request.run_id), 2)).state.outcome() is RunOutcome.CANCELLED
    assert (await adapter.wait_terminal(parent.child_run_id)).outcome is RunOutcome.CANCELLED


async def test_completed_child_does_not_block_parent_abandon(tmp_path):
    engine, repository, coordinator, adapter, _ = _stack(tmp_path, ParentChildLLM())
    request = replace(_node("parent"), agent_id="parent-agent")
    parent = await coordinator.submit(request)
    await adapter.wait_terminal(parent.child_run_id)
    assert (await coordinator.abandon_run(request.run_id)).state.outcome() is RunOutcome.ABANDONED
    assert (await repository.find_run(parent.child_run_id)).state.outcome() is RunOutcome.COMPLETED


@pytest.mark.parametrize("entry", ["engine", "coordinator"])
@pytest.mark.parametrize("winner", ["prepare", "abandon"])
async def test_public_child_prepare_serializes_with_parent_abandon(tmp_path, monkeypatch, entry, winner):
    """两边都走公开入口；父gate里的真实仓储屏障固定每个竞争顺序。"""
    engine, repository, coordinator, _, _ = _stack(tmp_path, BlockedLLM())
    await coordinator.prepare_run(_node("parent"))
    child = _node("child", "parent", "parent")
    entered, release = asyncio.Event(), asyncio.Event()
    target = engine if entry == "engine" else coordinator
    if winner == "prepare":
        original = repository.create_run
        async def create(run):
            if run.parent_run_id:
                entered.set()
                await release.wait()
            await original(run)
        monkeypatch.setattr(repository, "create_run", create)
        first = asyncio.create_task(target.prepare_run(child))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(coordinator.abandon_run("parent"))
        release.set()
        assert await first == "child"
        with pytest.raises(ValueError, match="先取消子树"):
            await second
        assert not (await repository.find_run("parent")).state.is_ended()
        await coordinator.cancel("parent", "竞争后收尾")
        await asyncio.wait_for(coordinator.wait_terminal("parent"), 2)
    else:
        original = repository.list_child_runs
        async def children(run_id):
            if run_id == "parent":
                entered.set()
                await release.wait()
            return await original(run_id)
        monkeypatch.setattr(repository, "list_child_runs", children)
        first = asyncio.create_task(coordinator.abandon_run("parent"))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(target.prepare_run(child))
        release.set()
        assert (await first).state.outcome() is RunOutcome.ABANDONED
        with pytest.raises(ValueError, match="父运行"):
            await second
        assert await repository.find_run("child") is None
    assert not engine._llm_port.started.is_set()


async def test_internal_child_prepare_refuses_busy_session_without_waiting_under_parent_gate(tmp_path):
    engine, _, coordinator, adapter, _ = _stack(tmp_path)
    request, submission = await _prepared_child(engine, adapter)
    lock = await coordinator._get_lock(submission.target_session_id)
    await lock.acquire()
    try:
        async with engine._cancellation_service.gate("parent"):
            # 已持久子Run直接校验返回，不重新进入其Session运行租约。
            assert (await asyncio.wait_for(adapter.prepare(request), 1)).child_run_id == submission.child_run_id
            with pytest.raises(SessionBusyError, match="不能持父gate等待"):
                await asyncio.wait_for(coordinator._prepare_child_run_locked(
                    replace(_node("another", "parent", "parent"), session_id=submission.target_session_id)), 1)
    finally:
        lock.release()
