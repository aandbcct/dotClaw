"""能力任务的所有权、取消竞争和资源清理边界。"""

from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dotclaw.common.resource_errors import ResourceCleanupError
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.execution import CancellationToken
from dotclaw.runtime.application.operation import RunOperationKind, RunOperationCancelled, RunOperationCleanupError
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.cancellation import CancellationRequest, CancellationOrigin, CancellationPhase, CancellationDisposition
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import RunMessage, RunMessageKind, MessageRole
from dotclaw.runtime.domain.state import RunOutcome
from tests.runtime_v2.test_runtime_engine import _engine, _request, ReActLLM
from dotclaw.runtime.adapters import run_repository as file_repository


def _cancel(run_id="run"):
    return CancellationRequest("request-" + run_id, run_id, "用户停止", CancellationOrigin.USER, "2026-11-06T00:00:00+00:00")


async def test_request_before_registration_does_not_call_factory() -> None:
    service = CancellationService()
    called = []
    async with service.gate("run"):
        service.signal(_cancel())
        token = CancellationToken()
        service.register_driver("run", token, _cancel())
    with pytest.raises(RunOperationCancelled):
        await service.run_operation("run", RunOperationKind.TOOL, lambda: called.append(True))
    assert called == []
    assert token.request_id == "request-run"
    assert not service.pending_operations("run")


async def test_duplicate_signal_does_not_interrupt_cleanup_and_other_run() -> None:
    service = CancellationService()
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    driver = asyncio.create_task(service.run_operation("run", RunOperationKind.TOOL, operation))
    await started.wait()
    async with service.gate("run"):
        service.signal(_cancel())
    await cleaning.wait()
    other = asyncio.create_task(service.run_operation("other", RunOperationKind.LLM, lambda: asyncio.sleep(0, result="正常")))
    async with service.gate("run"):
        service.signal(_cancel())
    assert not driver.done()
    assert await other == "正常"
    release.set()
    with pytest.raises(RunOperationCancelled):
        await driver
    assert not service.pending_operations("run")


async def test_normal_result_wins_child_cancel_race_and_remains_real() -> None:
    service = CancellationService()
    started = asyncio.Event()
    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "副作用已完成"
    driver = asyncio.create_task(service.run_operation("run", RunOperationKind.TOOL, operation))
    await started.wait()
    async with service.gate("run"):
        service.signal(_cancel())
    assert await driver == "副作用已完成"
    assert not service.pending_operations("run")


async def test_driver_system_cancel_keeps_cancelled_error_and_waits_cleanup() -> None:
    service = CancellationService()
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    driver = asyncio.create_task(service.run_operation("run", RunOperationKind.LLM, operation))
    await started.wait()
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await driver
    assert cleaned.is_set()
    assert service._requests == {}
    assert not service.pending_operations("run")


async def test_unrequested_child_cancel_is_failure_and_cleanup_failure_is_retained() -> None:
    service = CancellationService()
    async def self_cancel():
        raise asyncio.CancelledError()
    with pytest.raises(RuntimeError, match="无取消请求"):
        await service.run_operation("run", RunOperationKind.LLM, self_cancel)
    async def broken_cleanup():
        raise ResourceCleanupError("process", "process_alive", "进程仍存活")
    with pytest.raises(RunOperationCleanupError) as captured:
        await service.run_operation("run", RunOperationKind.TOOL, broken_cleanup)
    handles = service.pending_operations("run")
    assert len(handles) == 1 and handles[0].cleanup_error == "process_alive"
    assert captured.value.operation_id == handles[0].operation_id
    handles[0].cleanup_error = None
    assert service.pending_operations("run")[0].cleanup_error == "process_alive"
    with pytest.raises(ValueError):
        service.clear_settled("run")


async def test_uncancelled_cleanup_failure_blocks_resume_operation_and_success(tmp_path) -> None:
    """真实仓储中的非终态在资源失败后不能通过恢复业务变为成功。"""
    from dotclaw.runtime.application.execution import RunExecution, RunBudget
    from dotclaw.runtime.domain.state import AgentRunState, Ended
    engine = _engine(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def broken(*args):
        entered.set()
        await release.wait()
        raise ResourceCleanupError("http", "still_open", "连接未关闭")
    engine._llm_port = SimpleNamespace(complete=broken)
    driver = asyncio.create_task(engine.execute(_request("session")))
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    failed = await driver
    assert not failed.state.is_ended()
    run = await engine._run_repository.find_run(failed.run_id)
    assert await engine._run_repository.load_cancellation("session", run.run_id) is None
    handles = engine._cancellation_service.pending_operations(run.run_id)
    assert len(handles) == 1 and handles[0].cleanup_error == "still_open"
    calls = []
    async def normal(*args):
        calls.append(True)
        return RunMessage("final", 0, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "不应执行")
    engine._llm_port = SimpleNamespace(complete=normal)
    resumed = await engine.resume_run(run.run_id)
    assert not resumed.state.is_ended() and calls == []
    with pytest.raises(RunOperationCleanupError):
        await engine._cancellation_service.run_operation(run.run_id, RunOperationKind.LLM, normal)
    messages = await engine._run_repository.load_messages("session", run.run_id)
    final = replace(await normal(), sequence=len(messages) + 1)
    await engine._run_repository.save_messages("session", run.run_id, messages + (final,))
    execution = RunExecution(run.run_id, _request("session"), run.policy,
        AgentRunState(Ended(RunOutcome.COMPLETED)), RunBudget(run.policy.max_iterations))
    events = await engine._run_repository.load_events("session", run.run_id)
    with pytest.raises(RunOperationCleanupError):
        await engine._finalize_action(execution, run, final, len(events))
    assert not (await engine._run_repository.find_run(run.run_id)).state.is_ended()
    assert engine._cancellation_service.pending_operations(run.run_id)[0].cleanup_error == "still_open"
    assert await engine._run_repository.load_conversation("session") == ()
    assert not await engine._run_repository.has_pending_success_commit("session", run.run_id)


async def test_request_write_survives_repeated_outer_cancel_without_releasing_gate(tmp_path, monkeypatch) -> None:
    """请求文件写入已开始时外层取消，不能让成功提交越过仍在工作的文件线程。"""
    engine = _engine(tmp_path)
    model_started, model_release, model_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def model(*args):
        model_started.set()
        await model_release.wait()
        model_returned.set()
        return RunMessage("result", 0, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "实际返回")
    engine._llm_port = SimpleNamespace(complete=model)
    driver = asyncio.create_task(engine.execute(_request("session")))
    await asyncio.wait_for(model_started.wait(), 2)
    run = await engine.active_run("session")
    entered, release = threading.Event(), threading.Event()
    original = file_repository.write_json_atomic
    signal_records = []
    original_signal = engine._cancellation_service.signal
    def signal(request):
        signal_records.append(engine._run_repository._load_cancellation_sync("session", run.run_id))
        original_signal(request)
    def blocked_write(path, payload):
        if path.name == "cancellation.json" and payload.get("revision") == 1:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试未释放请求写入屏障")
        original(path, payload)
    monkeypatch.setattr(engine._cancellation_service, "signal", signal)
    monkeypatch.setattr(file_repository, "write_json_atomic", blocked_write)
    requester = asyncio.create_task(engine.request_cancel(run.run_id, "先持久化取消"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        requester.cancel()
        await asyncio.sleep(0)
        requester.cancel()
        await asyncio.sleep(0)
        assert not requester.done()
        assert engine._cancellation_service.gate(run.run_id).locked()
        model_release.set()
        await asyncio.wait_for(model_returned.wait(), 2)
        assert not driver.done()
    finally:
        release.set()
        model_release.set()
    with pytest.raises(asyncio.CancelledError):
        await requester
    result = await asyncio.wait_for(driver, 2)
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert signal_records and signal_records[0].request.reason == "先持久化取消"
    assert (await engine._run_repository.load_cancellation("session", run.run_id)).phase is CancellationPhase.SETTLED
    events = await engine._run_repository.load_events("session", run.run_id)
    assert sum(event.event_type is RunEventType.RUN_CANCELLED for event in events) == 1
    assert not any(event.event_type is RunEventType.RUN_COMPLETED for event in events)
    assert await engine._run_repository.load_conversation("session") == ()


@pytest.mark.parametrize("commit_file", ["success_commit.json", "cancel_commit.json"])
async def test_terminal_commit_holds_gate_until_file_thread_finishes_after_driver_cancel(tmp_path, monkeypatch, commit_file) -> None:
    """终态意图已开始写入后，驱动取消也不能提前释放裁决锁。"""
    engine = _engine(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = file_repository.write_json_atomic
    def blocked_write(path, payload):
        if path.name == commit_file:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试未释放成功提交屏障")
        original(path, payload)
    monkeypatch.setattr(file_repository, "write_json_atomic", blocked_write)
    async def accepted(event):
        await engine.request_cancel(event.run_id, "受理后停止")
    output = SimpleNamespace(on_run_accepted=accepted) if commit_file == "cancel_commit.json" else None
    expected = RunOutcome.CANCELLED if output is not None else RunOutcome.COMPLETED
    driver = asyncio.create_task(engine.execute(_request("session"), output))
    requester = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        run = (await engine._run_repository.list_active_runs("session"))[0]
        driver.cancel()
        await asyncio.sleep(0)
        driver.cancel()
        await asyncio.sleep(0)
        assert not driver.done() and engine._cancellation_service.gate(run.run_id).locked()
        requester = asyncio.create_task(engine.request_cancel(run.run_id, "竞争取消"))
        await asyncio.sleep(0)
        assert not requester.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await driver
    receipt = await asyncio.wait_for(requester, 2)
    assert receipt.disposition is CancellationDisposition.ALREADY_ENDED
    assert receipt.outcome is expected
    record = await engine._run_repository.load_cancellation("session", run.run_id)
    assert (record is None) is (expected is RunOutcome.COMPLETED)
    if record is not None:
        assert record.phase is CancellationPhase.SETTLED
    assert (await engine._run_repository.find_run(run.run_id)).state.outcome() is expected
    events = await engine._run_repository.load_events("session", run.run_id)
    expected_event = RunEventType.RUN_CANCELLED if output is not None else RunEventType.RUN_COMPLETED
    assert sum(event.event_type is expected_event for event in events) == 1
    assert sum(event.event_type in (RunEventType.RUN_COMPLETED, RunEventType.RUN_CANCELLED) for event in events) == 1


async def test_duplicate_driver_and_first_request_are_immutable() -> None:
    service, token = CancellationService(), CancellationToken()
    async with service.gate("run"):
        service.register_driver("run", token, _cancel())
        with pytest.raises(ValueError):
            service.register_driver("run", CancellationToken(), None)
        service.signal(CancellationRequest("second", "run", "覆盖", CancellationOrigin.USER, "2026-11-06T01:00:00Z"))
    assert token.reason == "用户停止" and token.request_id == "request-run"


@pytest.mark.parametrize("boundary", ["llm", "context"])
async def test_runtime_cancel_does_not_cancel_driver_or_start_next_action(tmp_path, boundary) -> None:
    notifier = RunChangeNotifier()
    engine = _engine(tmp_path, run_change_notifier=notifier)
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def blocked(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    if boundary == "llm":
        engine._llm_port = SimpleNamespace(complete=blocked)
    else:
        engine._context_port.build = blocked
    driver = asyncio.create_task(coordinator.submit(_request("session")))
    await asyncio.wait_for(started.wait(), 2)
    run = await engine.active_run("session")
    receipt = await asyncio.wait_for(coordinator.cancel(run.run_id, "用户停止"), 1)
    assert receipt.disposition is CancellationDisposition.ACCEPTED
    result = await asyncio.wait_for(driver, 2)
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert not driver.cancelled() and cleaned.is_set()
    events = await engine._run_repository.load_events("session", run.run_id)
    assert sum(event.event_type is RunEventType.RUN_CANCELLED for event in events) == 1
    assert await engine._run_repository.load_conversation("session") == ()
    if boundary == "context":
        assert not any(event.event_type is RunEventType.LLM_STARTED for event in events)


async def test_tool_completed_during_cancel_records_real_output_and_skips_remaining_tools(tmp_path) -> None:
    notifier = RunChangeNotifier()
    engine = _engine(tmp_path, run_change_notifier=notifier)
    engine._llm_port = ReActLLM()
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    started = asyncio.Event()
    calls = []
    async def tool(invocation, execution):
        from dotclaw.runtime.application.dto import ToolResult, ToolResultStatus
        calls.append(invocation.call.call_id)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, "实际完成")
    engine._tool_port = SimpleNamespace(execute=tool)
    driver = asyncio.create_task(coordinator.submit(_request("session")))
    await asyncio.wait_for(started.wait(), 2)
    run = await engine.active_run("session")
    await coordinator.cancel(run.run_id, "停止")
    assert (await driver).state.outcome() is RunOutcome.CANCELLED
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    assert calls == ["call-1"]
    messages = await engine._run_repository.load_messages("session", run.run_id)
    assert any(message.content == "实际完成" for message in messages)
    events = await engine._run_repository.load_events("session", run.run_id)
    completed = [event for event in events if event.event_type is RunEventType.TOOL_COMPLETED]
    assert len(completed) == 1 and completed[0].data["status"] == "completed"
    assert not any(event.data.get("call_id") == "call-2" for event in events)


async def test_cleanup_failure_keeps_run_nonterminal_and_session_occupied(tmp_path) -> None:
    notifier = RunChangeNotifier()
    engine = _engine(tmp_path, run_change_notifier=notifier)
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    started = asyncio.Event()
    async def broken_model(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise ResourceCleanupError("http", "connection_alive", "连接仍未关闭")
    engine._llm_port = SimpleNamespace(complete=broken_model)
    driver = asyncio.create_task(coordinator.submit(_request("session")))
    await asyncio.wait_for(started.wait(), 2)
    run = await engine.active_run("session")
    await coordinator.cancel(run.run_id, "停止")
    result = await driver
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))
    assert not result.state.is_ended()
    assert (await engine.active_run("session")).run_id == run.run_id
    record = await engine._run_repository.load_cancellation("session", run.run_id)
    assert record.phase is CancellationPhase.BLOCKED
    assert record.diagnostic_code == "connection_alive"
    assert not any(event.event_type is RunEventType.RUN_CANCELLED for event in await engine._run_repository.load_events("session", run.run_id))


async def test_slow_cleanup_is_reported_blocked_without_recancelling_finally(tmp_path) -> None:
    notifier = RunChangeNotifier()
    engine = _engine(tmp_path, run_change_notifier=notifier)
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def slow_model(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    engine._llm_port = SimpleNamespace(complete=slow_model)
    driver = asyncio.create_task(coordinator.submit(_request("session")))
    await asyncio.wait_for(started.wait(), 2)
    run = await engine.active_run("session")
    receipt = await coordinator.cancel(run.run_id, "停止")
    await cleaning.wait()
    version = engine._run_change_notifier.version(run.run_id)
    await engine._run_change_notifier.wait(run.run_id, version, 6)
    record = await engine._run_repository.load_cancellation("session", run.run_id)
    assert record.phase is CancellationPhase.BLOCKED and record.diagnostic_code == "cleanup_pending"
    assert not driver.done()
    repeated = await coordinator.cancel(run.run_id, "再次停止")
    assert repeated.request_id == receipt.request_id
    assert not driver.done()
    release.set()
    assert (await driver).state.outcome() is RunOutcome.CANCELLED
    await asyncio.gather(*tuple(coordinator._cancellation_jobs.values()))


async def test_accepted_callback_cancellation_before_operation_registration(tmp_path) -> None:
    engine = _engine(tmp_path)
    called = []
    async def complete(*args):
        called.append(True)
    engine._llm_port = SimpleNamespace(complete=complete)
    async def accepted(event):
        await engine.request_cancel(event.run_id, "受理时停止")
    result = await engine.execute(_request("session"), SimpleNamespace(on_run_accepted=accepted))
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert called == []


async def test_completion_replays_frozen_intent_and_clears_projection(tmp_path, monkeypatch) -> None:
    """提交删除意图前中断后，单运行补偿仍须清理内存并发布变更提示。"""
    engine = _engine(tmp_path)
    async def accepted(event):
        await engine.request_cancel(event.run_id, "受理后停止")
    def failed(*args):
        raise OSError("删除提交意图前中断")
    with monkeypatch.context() as patch:
        patch.setattr(engine._run_repository, "_delete_cancellation_intent_sync", failed)
        with pytest.raises(OSError):
            await engine.execute(_request("session"), SimpleNamespace(on_run_accepted=accepted))
    run_id = next(iter(engine._cancellation_service._requests))
    version = engine._run_change_notifier.version(run_id)
    result = await engine.complete_cancellation(run_id)
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert run_id not in engine._cancellation_service._requests
    assert engine._run_change_notifier.version(run_id) > version
    assert not await engine._run_repository.has_pending_cancellation_commit("session", run_id)


async def test_cancelled_approval_never_executes_approved_tool(tmp_path) -> None:
    from tests.runtime_v2.test_runtime_engine import ApprovalLLM, ApprovalTool
    engine = _engine(tmp_path)
    engine._llm_port = ApprovalLLM()
    tool = ApprovalTool()
    engine._tool_port = tool
    waiting = await engine.execute(_request("session"))
    await engine.request_cancel(waiting.run_id, "先取消")
    result = await engine.resolve_approval("approval-1", True)
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert tool._call_count == 1
