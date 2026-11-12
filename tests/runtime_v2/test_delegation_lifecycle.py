"""两阶段委派、事实观察和宿主恢复的持久化故障窗口。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.orchestration.dispatcher import AgentDispatcher
from dotclaw.orchestration.message_broker import TaskMessageBroker, TaskStateError
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.orchestration.runtime_delegation_adapter import RuntimeDelegationAdapter
from dotclaw.orchestration.task import TaskStatus
from dotclaw.runtime.adapters import CheckpointRepositoryAdapter
from dotclaw.runtime.application.dto import DelegationRequest
from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.cancellation import CancellationOrigin, CancellationPhase, CancellationEffect
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.state import AgentRunState, Created, Running, RunOutcome, RunStage
from dotclaw.session.session import SessionManager
from tests.runtime_v2.test_runtime_engine import _engine, _request, BlockedLLM, ApprovalLLM, ApprovalTool
from tests.runtime_v2.test_delegation_port import ParentChildLLM
from tests.runtime_v2.test_e5_success_commit_recovery import _prepare_pending_success, FaultInjector, SuccessCommitInterrupted
from dotclaw.runtime.domain.context import SuccessCommitFaultPoint
from dotclaw.runtime.application.operation import RunOperationKind


def _stack(root, model=None):
    """每个实例只装配一个通知器、取消服务和驱动协调器。"""
    notifier = RunChangeNotifier()
    engine = _engine(root, run_change_notifier=notifier)
    if model is not None:
        engine._llm_port = model
    repository = engine._run_repository
    registry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="target-agent", agent_name="子运行"))
    dispatcher = AgentDispatcher(TaskMessageBroker())
    adapter = RuntimeDelegationAdapter(SessionManager(root), registry, dispatcher, "model", run_repository=repository)
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    adapter.bind_coordinator(coordinator)
    engine._delegation_port = adapter
    return engine, repository, coordinator, adapter, dispatcher


def _node(identifier, parent=None, root=None):
    """不同节点使用不同 Session，持久关联不依赖业务 Task 缓存。"""
    return replace(_request("session-" + identifier), run_id=identifier, parent_run_id=parent, root_run_id=root)


async def _prepared_child(engine, adapter):
    parent = replace(_node("parent"), agent_id="parent-agent")
    await engine.prepare_run(parent)
    request = DelegationRequest("parent", "parent", "target-agent", parent.user_message,
        source_agent_id="parent-agent", source_session_id=parent.session_id, source_tool_call_id="delegate-one")
    async with engine._cancellation_service.gate(parent.run_id):
        return request, await adapter.prepare(request)


async def test_prepare_is_local_idempotent_and_start_clears_frozen_request(tmp_path):
    """准备不进入模型；重复安排只存在一个真实能力和一次受理。"""
    model = BlockedLLM()
    engine, repository, coordinator, _, _ = _stack(tmp_path, model)
    request = _node("prepared")
    assert await coordinator.prepare_run(request) == "prepared"
    assert await coordinator.prepare_run(request) == "prepared"
    assert not model.started.is_set()
    run = await repository.find_run("prepared")
    assert isinstance(run.state.mode, Created)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load(request.session_id, request.run_id)
    assert checkpoint.pending["prepared_request"] == request.to_dict()
    assert len(await repository.load_messages(request.session_id, request.run_id)) == 1
    coordinator.schedule_prepared(request.run_id)
    observer = asyncio.create_task(coordinator.start_prepared(request.run_id))
    await asyncio.wait_for(model.started.wait(), 2)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load(request.session_id, request.run_id)
    assert "prepared_request" not in checkpoint.pending
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert not model.cancel_observed.is_set()
    model.release.set()
    result = await asyncio.wait_for(coordinator.wait_terminal(request.run_id), 2)
    assert result.state.outcome() is RunOutcome.COMPLETED
    events = await repository.load_events(request.session_id, request.run_id)
    assert sum(event.event_type is RunEventType.RUN_STARTED for event in events) == 1
    assert len([message for message in await repository.load_messages(request.session_id, request.run_id)
                if message.kind.value == "user_input"]) == 1


@pytest.mark.parametrize("field", ["run_id", "session_id", "agent_id", "parent_run_id", "root_run_id", "delegation_task_id", "user_id", "user_content"])
async def test_prepared_request_identity_and_created_state_are_checked(tmp_path, field):
    """错归属准备请求不启动能力；运行后的请求不能再次准备。"""
    engine, repository, _, _, _ = _stack(tmp_path)
    request = _node("prepared")
    await engine.prepare_run(request)
    checkpoints = CheckpointRepositoryAdapter(tmp_path)
    checkpoint = await checkpoints.load(request.session_id, request.run_id)
    bad = dict(checkpoint.pending["prepared_request"])
    if field.startswith("user_"):
        bad["user_message"] = {**bad["user_message"], "id" if field == "user_id" else "content": "foreign"}
    else:
        bad[field] = "foreign"
    if field in {"run_id", "session_id"}:
        with pytest.raises(ValueError):
            await checkpoints.save(replace(checkpoint, pending={"prepared_request": bad}))
    else:
        await checkpoints.save(replace(checkpoint, pending={"prepared_request": bad}))
        with pytest.raises(ValueError, match="归属"):
            await engine.start_prepared(request.run_id)
    run = await repository.find_run(request.run_id)
    assert isinstance(run.state.mode, Created)
    await repository.save_run(replace(run, state=AgentRunState(Running(RunStage.CALLING_LLM))))
    with pytest.raises(ValueError):
        await engine.prepare_run(request)
    with pytest.raises(ValueError, match="Created"):
        await engine.start_prepared(request.run_id)


async def test_cancel_before_start_never_calls_model(tmp_path):
    engine, repository, coordinator, _, _ = _stack(tmp_path, BlockedLLM())
    request = _node("prepared")
    await coordinator.prepare_run(request)
    await coordinator.cancel(request.run_id, "准备后停止")
    result = await asyncio.wait_for(coordinator.wait_terminal(request.run_id), 2)
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert not engine._llm_port.started.is_set()
    events = await repository.load_events(request.session_id, request.run_id)
    assert not any(event.event_type in (RunEventType.RUN_STARTED, RunEventType.LLM_STARTED) for event in events)


@pytest.mark.parametrize("extra", ["pending", "conversation", "history"])
async def test_prepared_checkpoint_exception_accepts_only_frozen_input_structure(tmp_path, extra):
    engine, _, _, _, _ = _stack(tmp_path)
    request = _node("prepared")
    await engine.prepare_run(request)
    repository = CheckpointRepositoryAdapter(tmp_path)
    checkpoint = await repository.load(request.session_id, request.run_id)
    pending = json.loads(json.dumps(checkpoint.pending))
    if extra == "pending":
        pending["messages"] = []
    elif extra == "conversation":
        pending["prepared_request"]["conversation"]["prompt"] = "不属于冻结输入结构"
    else:
        pending["prepared_request"]["conversation"]["compressed_history"] = {"messages": []}
    with pytest.raises(ValueError):
        await repository.save(replace(checkpoint, pending=pending))


async def test_parent_child_grandchild_cancel_and_independent_branch(tmp_path):
    """仅取消目标子树；父请求因果链和真实终态贯穿孙运行。"""
    model = BlockedLLM()
    engine, repository, coordinator, _, _ = _stack(tmp_path, model)
    requests = [_node("parent"), _node("child", "parent", "parent"),
                _node("grandchild", "child", "parent"), _node("unrelated")]
    for request in requests:
        await coordinator.prepare_run(request)
    coordinator.schedule_prepared("grandchild")
    await asyncio.wait_for(model.started.wait(), 2)
    await coordinator.cancel("parent", "取消整棵子树")
    result = await asyncio.wait_for(coordinator.wait_terminal("parent"), 3)
    assert result.state.outcome() is RunOutcome.CANCELLED
    records = [await repository.load_cancellation(request.session_id, request.run_id) for request in requests[:3]]
    assert all(record.phase is CancellationPhase.SETTLED for record in records)
    assert records[1].request.parent_request_id == records[0].request.request_id
    assert records[2].request.parent_request_id == records[1].request.request_id
    assert records[1].request.origin is CancellationOrigin.PARENT
    assert isinstance((await repository.find_run("unrelated")).state.mode, Created)
    assert await repository.load_cancellation("session-unrelated", "unrelated") is None
    assert model.cleanup_completed.is_set()


async def test_completed_child_fact_survives_parent_cancel(tmp_path):
    engine, repository, coordinator, adapter, dispatcher = _stack(tmp_path)
    _, submission = await _prepared_child(engine, adapter)
    await adapter.start(submission.child_run_id)
    await adapter.wait_terminal(submission.child_run_id)
    await coordinator.cancel("parent", "子成功后取消父")
    await asyncio.wait_for(coordinator.wait_terminal("parent"), 2)
    child = await repository.find_run(submission.child_run_id)
    assert child.state.outcome() is RunOutcome.COMPLETED
    task = await dispatcher.broker.latest_task_for_source("session-parent")
    assert task.status is TaskStatus.COMPLETED


async def test_approval_child_cancel_consumes_approval_without_resume(tmp_path):
    engine, repository, coordinator, _, _ = _stack(tmp_path, ApprovalLLM())
    engine._tool_port = ApprovalTool()
    await coordinator.prepare_run(_node("parent"))
    await coordinator.prepare_run(_node("child", "parent", "parent"))
    suspended = await coordinator.start_prepared("child")
    assert suspended.approval_id
    await coordinator.cancel("parent", "审批等待中停止")
    await asyncio.wait_for(coordinator.wait_terminal("parent"), 2)
    rejected = await coordinator.resolve_approval(suspended.approval_id, True)
    assert rejected.state.outcome() is not RunOutcome.COMPLETED
    assert (await repository.find_run("child")).state.outcome() is RunOutcome.CANCELLED


async def test_restore_fixed_projection_without_fake_messages_and_reject_conflicts(tmp_path):
    engine, repository, _, adapter, _ = _stack(tmp_path)
    request, submission = await _prepared_child(engine, adapter)
    _, _, restored_coordinator, restored_adapter, dispatcher = _stack(tmp_path)
    assert await restored_adapter.result(submission.child_run_id) is None
    task = await dispatcher.broker.latest_task_for_source("session-parent")
    assert task.task_id == submission.task_id and not task.status.is_terminal()
    assert not dispatcher.broker._messages[task.task_id]
    assert (await restored_adapter.prepare(request)).child_run_id == submission.child_run_id
    with pytest.raises(ValueError, match="冲突"):
        await restored_adapter.prepare(replace(request, input_message=replace(request.input_message, content="另一任务")))
    with pytest.raises(TaskStateError, match="冲突"):
        await dispatcher.restore_v2_delegation(task.task_id, "foreign", "session-parent", "target-agent", task.target.session_id, request.input_message.content)
    await restored_coordinator.cancel(submission.child_run_id, "独立取消子分支")
    await restored_adapter.wait_terminal(submission.child_run_id)
    assert isinstance((await repository.find_run("parent")).state.mode, Created)
    await restored_adapter.result(submission.child_run_id)
    assert len(dispatcher.broker._messages[task.task_id]) == 1
    with pytest.raises(TaskStateError, match="终态"):
        await dispatcher.settle_v2_delegation(task.task_id, submission.child_run_id, RunOutcome.COMPLETED, "伪成功")


@pytest.mark.parametrize("failure", ["before_create", "after_create"])
async def test_prepare_failure_compensates_only_new_session_and_never_duplicates_child(tmp_path, monkeypatch, failure):
    engine, repository, coordinator, adapter, dispatcher = _stack(tmp_path)
    parent = replace(_node("parent"), agent_id="parent-agent")
    await engine.prepare_run(parent)
    request = DelegationRequest("parent", "parent", "target-agent", parent.user_message,
        source_agent_id="parent-agent", source_session_id=parent.session_id)
    original = coordinator._prepare_child_run_locked
    async def fail(request):
        if failure == "after_create":
            await original(request)
        raise OSError("准备故障屏障")
    monkeypatch.setattr(coordinator, "_prepare_child_run_locked", fail)
    async with engine._cancellation_service.gate(parent.run_id):
        with pytest.raises(OSError):
            await adapter.prepare(request)
    children = await repository.list_child_runs("parent")
    task = await dispatcher.broker.latest_task_for_source(parent.session_id)
    if failure == "before_create":
        assert not children and task.status is TaskStatus.FAILED
        assert not (tmp_path / task.target.session_id).exists()
    else:
        assert len(children) == 1
        await asyncio.wait_for(coordinator.wait_terminal(children[0].run_id), 2)
        assert (await adapter.prepare(request)).child_run_id == children[0].run_id
        assert len(await repository.list_child_runs("parent")) == 1


async def test_prepare_parent_gate_persists_association_before_cancel_and_start(tmp_path, monkeypatch):
    """真实 checkpoint 保存屏障覆盖子准备完成至父关联落盘前窗口。"""
    engine, repository, coordinator, adapter, _ = _stack(tmp_path, ParentChildLLM())
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine._checkpoint_repository.save
    async def save(checkpoint):
        await original(checkpoint)
        if "prepared_request" in checkpoint.pending and checkpoint.pending["prepared_request"]["parent_run_id"]:
            entered.set()
            await release.wait()
    monkeypatch.setattr(engine._checkpoint_repository, "save", save)
    parent_task = asyncio.create_task(coordinator.submit(replace(_node("parent"), agent_id="parent-agent")))
    await asyncio.wait_for(entered.wait(), 2)
    cancel = asyncio.create_task(coordinator.cancel("parent", "准备屏障期间取消"))
    assert not cancel.done()
    release.set()
    await asyncio.wait_for(cancel, 2)
    await asyncio.wait_for(parent_task, 2)
    await asyncio.wait_for(coordinator.wait_terminal("parent"), 2)
    children = await repository.list_child_runs("parent")
    assert len(children) == 1
    assert children[0].state.outcome() is RunOutcome.CANCELLED
    events = await repository.load_events(children[0].session_id, children[0].run_id)
    assert not any(event.event_type is RunEventType.LLM_STARTED for event in events)


@pytest.mark.parametrize("boundary", ["child_checkpoint", "parent_checkpoint", "start"])
async def test_handoff_fault_windows_stop_prepared_children_without_running_them(tmp_path, monkeypatch, boundary):
    """准备后或挂起后失败均保留唯一子事实，补偿不启动外部能力。"""
    engine, repository, coordinator, adapter, dispatcher = _stack(tmp_path, ParentChildLLM())
    original = engine._checkpoint_repository.save
    failed = False
    async def save(checkpoint):
        nonlocal failed
        is_child = "prepared_request" in checkpoint.pending and checkpoint.pending["prepared_request"]["parent_run_id"]
        is_parent = "child_run_id" in checkpoint.pending
        if not failed and ((boundary == "child_checkpoint" and is_child) or (boundary == "parent_checkpoint" and is_parent)):
            failed = True
            raise OSError("checkpoint故障窗口")
        await original(checkpoint)
    async def fail_start(child_run_id):
        raise OSError("启动安排故障窗口")
    monkeypatch.setattr(engine._checkpoint_repository, "save", save)
    if boundary == "start":
        monkeypatch.setattr(adapter, "start", fail_start)
    await coordinator.submit(replace(_node("parent"), agent_id="parent-agent"))
    children = await repository.list_child_runs("parent")
    assert len(children) == 1
    child = children[0]
    await asyncio.wait_for(adapter.wait_terminal(child.run_id), 2)
    events = await repository.load_events(child.session_id, child.run_id)
    assert not any(event.event_type is RunEventType.LLM_STARTED for event in events)
    assert (await repository.find_run(child.run_id)).state.outcome() is RunOutcome.CANCELLED
    task = await dispatcher.broker.latest_task_for_source("session-parent")
    assert task.status is TaskStatus.CANCELLED


async def test_manual_confirmation_refuses_a_live_resource_and_registered_driver(tmp_path):
    """请求取消与BLOCKED不足以证明退出；维护入口仍检查真实句柄和驱动。"""
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    request = _node("maintenance")
    await engine.prepare_run(request)
    service = engine._cancellation_service
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
    capability = asyncio.create_task(service.run_operation(request.run_id, RunOperationKind.TOOL, operation))
    await entered.wait()
    await engine.request_cancel(request.run_id, "人工维护前停止")
    await draining.wait()
    record = await repository.load_cancellation(request.session_id, request.run_id)
    await repository.save_cancellation(request.session_id, replace(record, revision=record.revision + 1,
        phase=CancellationPhase.BLOCKED, diagnostic_code="cleanup_pending"), expected_revision=record.revision)
    with pytest.raises(ValueError, match="在途"):
        await coordinator.confirm_cancellation_cleanup(request.run_id, "进程尚未停")
    release.set()
    await asyncio.gather(capability, return_exceptions=True)
    async with service.gate(request.run_id):
        service._drivers.add(request.run_id)
    with pytest.raises(ValueError, match="驱动"):
        await coordinator.confirm_cancellation_cleanup(request.run_id, "驱动尚未退出")
    service.unregister_driver(request.run_id)


async def test_shutdown_timeout_keeps_resource_and_run_nonterminal(tmp_path):
    """不可强杀的清理超过关闭宽限时保留事实，不能通过取消作业Task假装停止。"""
    model = BlockedLLM()
    engine, repository, coordinator, _, _ = _stack(tmp_path, model)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = model.complete
    async def complete(context, execution, output_port=None):
        entered.set()
        try:
            return await original(context, execution, output_port)
        finally:
            cleaning.set()
            await release.wait()
    model.complete = complete
    request = _node("slow-close")
    work = asyncio.create_task(coordinator.submit(request))
    await entered.wait()
    await asyncio.wait_for(coordinator.shutdown(0.01), 2)
    assert cleaning.is_set() and not work.done()
    assert not (await repository.find_run(request.run_id)).state.is_ended()
    assert engine._cancellation_service.pending_operations(request.run_id)
    release.set()
    await asyncio.wait_for(work, 2)
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    assert (await repository.find_run(request.run_id)).state.outcome() is RunOutcome.CANCELLED


async def test_restart_prepared_child_is_cancelled_not_started(tmp_path):
    engine, _, _, adapter, _ = _stack(tmp_path)
    _, submission = await _prepared_child(engine, adapter)
    recovered, repository, coordinator, restored_adapter, dispatcher = _stack(tmp_path, BlockedLLM())
    await coordinator.recover_cancellations()
    await asyncio.wait_for(restored_adapter.wait_terminal(submission.child_run_id), 2)
    assert not recovered._llm_port.started.is_set()
    assert isinstance((await repository.find_run("parent")).state.mode, Created)
    task = await dispatcher.broker.latest_task_for_source("session-parent")
    assert task.task_id == submission.task_id and task.status is TaskStatus.CANCELLED


@pytest.mark.parametrize("tool_name,effect", [("builtin.process.execute", CancellationEffect.LOCAL_ONLY),
    ("thirdparty.network_operation", CancellationEffect.REMOTE_UNKNOWN)])
async def test_restart_unknown_command_remains_blocked_and_manual_confirmation_is_audited(tmp_path, monkeypatch, tool_name, effect):
    engine, repository, coordinator, _, _ = _stack(tmp_path)
    request = _node("orphan")
    await engine.prepare_run(request)
    run = await repository.find_run(request.run_id)
    await repository.save_run(replace(run, state=AgentRunState(Running(RunStage.EXECUTING_TOOLS))))
    await repository.append_event(request.session_id, RunEvent(request.run_id, 1, RunEventType.TOOL_STARTED, "2026-11-07T00:00:00+00:00", data={"call_id": "command", "tool_name": tool_name}))
    with pytest.raises(ValueError):
        await coordinator.confirm_cancellation_cleanup(request.run_id, "尚未请求取消")
    await engine.request_cancel(request.run_id, "准备重启")
    recovered, repository, coordinator, _, _ = _stack(tmp_path)
    await coordinator.recover_cancellations()
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    record = await repository.load_cancellation(request.session_id, request.run_id)
    assert record.phase is CancellationPhase.BLOCKED
    assert record.effect is effect
    assert not (await repository.find_run(request.run_id)).state.is_ended()
    original = repository.append_event
    async def fail(session_id, event):
        if event.event_type is RunEventType.CANCELLATION_CLEANUP_CONFIRMED:
            raise OSError("审计写盘失败")
        await original(session_id, event)
    monkeypatch.setattr(repository, "append_event", fail)
    with pytest.raises(OSError):
        await coordinator.confirm_cancellation_cleanup(request.run_id, "人工确认已停止")
    assert (await repository.load_cancellation(request.session_id, request.run_id)).phase is CancellationPhase.BLOCKED
    monkeypatch.setattr(repository, "append_event", original)
    result = await coordinator.confirm_cancellation_cleanup(request.run_id, "人工确认本地进程已经停止")
    assert result.state.outcome() is RunOutcome.CANCELLED
    events = await repository.load_events(request.session_id, request.run_id)
    confirmation = next(event for event in events if event.event_type is RunEventType.CANCELLATION_CLEANUP_CONFIRMED)
    terminal = next(event for event in events if event.event_type is RunEventType.RUN_CANCELLED)
    assert confirmation.sequence < terminal.sequence and confirmation.data["manual"] is True


async def test_shutdown_stops_drivers_before_resource_close_and_refuses_new_work(tmp_path):
    model = BlockedLLM()
    engine, repository, coordinator, _, _ = _stack(tmp_path, model)
    request = _node("shutdown")
    work = asyncio.create_task(coordinator.submit(request))
    await asyncio.wait_for(model.started.wait(), 2)
    await coordinator.shutdown(2)
    await work
    assert model.cancel_observed.is_set() and model.cleanup_completed.is_set()
    record = await repository.load_cancellation(request.session_id, request.run_id)
    assert record.phase is CancellationPhase.SETTLED and record.request.origin is CancellationOrigin.HOST_SHUTDOWN
    with pytest.raises(RuntimeError, match="停止"):
        await coordinator.submit(_node("new"))


async def test_find_and_result_queries_do_not_recover_another_run(tmp_path):
    injector = FaultInjector(SuccessCommitFaultPoint.BEFORE_SESSION_PROJECTION)
    repository, checkpoints, _, completed, final, event, intent = await _prepare_pending_success(tmp_path, injector)
    with pytest.raises(SuccessCommitInterrupted):
        await repository.commit_success(completed, final, event, intent)
    engine, _, coordinator, _, _ = _stack(tmp_path)
    await engine.prepare_run(_node("other"))
    before = (tmp_path / completed.session_id / "agent_runs" / completed.run_id / "run.json").read_bytes()
    await asyncio.gather(*(engine.get_run_result("other") for _ in range(3)), repository.find_run(completed.run_id))
    assert (tmp_path / completed.session_id / "agent_runs" / completed.run_id / "run.json").read_bytes() == before
    assert await repository.has_pending_success_commit(completed.session_id, completed.run_id)
    assert not (await engine.get_run_result(completed.run_id)).state.is_ended()
    injector.enabled = False
    await repository.recover_pending_success_commits()
    assert (await engine.get_run_result(completed.run_id)).state.outcome() is RunOutcome.COMPLETED


async def test_v4_without_delegation_binding_loads_none_and_list_is_stable(tmp_path):
    engine, repository, _, _, _ = _stack(tmp_path)
    for name in ("z", "a"):
        await engine.prepare_run(_node(name))
    path = tmp_path / "session-z" / "agent_runs" / "z" / "run.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("delegation_task_id")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert (await repository.find_run("z")).delegation_task_id is None
    runs = await repository.list_runs()
    assert runs == tuple(sorted(runs, key=lambda run: (run.started_at, run.run_id)))
