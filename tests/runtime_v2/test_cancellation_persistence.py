"""取消记录、提交恢复与成功竞争的阶段 A 契约。"""

import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dotclaw.runtime.adapters import RunRepositoryAdapter, InMemoryRunRepository
from dotclaw.runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from dotclaw.runtime.adapters import run_repository as file_module
from dotclaw.runtime.domain.cancellation import (
    CancellationRequest, CancellationOrigin, CancellationPhase, CancellationEffect,
    CancellationRecord, CancellationCommitIntent,
)
from dotclaw.runtime.domain.context import SuccessCommitIntent
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import AgentRun, AgentPolicySnapshot, ApprovalRecord, ApprovalStatus
from dotclaw.runtime.domain.state import AgentRunState, Running, RunStage, Ended, RunOutcome


NOW = "2026-11-06T00:00:00+00:00"


def _run() -> AgentRun:
    return AgentRun("run", "session", "agent", AgentRunState(Running(RunStage.CALLING_LLM)), NOW,
                    AgentPolicySnapshot("agent", "identity", "model", 4), "input")


def _record() -> CancellationRecord:
    return CancellationRecord(CancellationRequest("request", "run", "停止", CancellationOrigin.USER, NOW),
                              1, CancellationPhase.REQUESTED, (), (), CancellationEffect.LOCAL_ONLY, None, NOW)


def _intent(run: AgentRun, record: CancellationRecord, approvals: tuple[str, ...] = ()) -> CancellationCommitIntent:
    settled = replace(record, revision=record.revision + 1, phase=CancellationPhase.SETTLED, settled_at=NOW)
    cancelled = replace(run, state=AgentRunState(Ended(RunOutcome.CANCELLED)), ended_at=NOW)
    events = (RunEvent(run.run_id, 1, RunEventType.CANCEL_REQUESTED, NOW),
              RunEvent(run.run_id, 2, RunEventType.RUN_CANCELLED, NOW))
    return CancellationCommitIntent(record.request.request_id, cancelled, events, approvals, settled)


@pytest.fixture(params=["file", "memory"])
def repository(request, tmp_path):
    return RunRepositoryAdapter(tmp_path) if request.param == "file" else InMemoryRunRepository()


async def test_revision_request_identity_and_settlement(repository) -> None:
    run, record = _run(), _record()
    await repository.create_run(run)
    assert await repository.load_cancellation("session", "run") is None
    await repository.save_cancellation("session", record, expected_revision=0)
    assert await repository.list_pending_cancellations() == (record,)
    with pytest.raises(ValueError):
        await repository.save_cancellation("session", record, expected_revision=0)
    with pytest.raises(ValueError):
        await repository.save_cancellation("session", replace(record, revision=2), expected_revision=1)
    with pytest.raises(ValueError):
        await repository.save_cancellation("session", replace(record, request=replace(record.request, reason="覆盖"), revision=2,
                                           phase=CancellationPhase.DRAINING), expected_revision=1)
    with pytest.raises(FileNotFoundError):
        await repository.save_cancellation("wrong", record, expected_revision=0)
    intent = _intent(run, record)
    await repository.commit_cancellation(intent)
    await repository.commit_cancellation(intent)
    await repository.recover_pending_cancellation_commits()
    assert await repository.load_run("session", "run") == intent.run
    assert await repository.load_events("session", "run") == intent.events
    assert await repository.load_cancellation("session", "run") == intent.settled_record
    assert await repository.list_pending_cancellations() == ()


async def test_child_query_uses_persisted_relationship(repository) -> None:
    child = replace(_run(), run_id="child", session_id="child-session", parent_run_id="run")
    await repository.create_run(_run())
    await repository.create_run(child)
    assert await repository.list_child_runs("run") == (child,)


async def test_single_run_recovery_never_replays_other_runs(repository, monkeypatch) -> None:
    run, record = _run(), _record()
    other = replace(run, run_id="other")
    other_record = replace(record, request=replace(record.request, run_id="other", request_id="other-request"))
    await repository.create_run(run)
    await repository.create_run(other)
    await repository.save_cancellation("session", record, expected_revision=0)
    await repository.save_cancellation("session", other_record, expected_revision=0)
    async def failed(intent):
        raise OSError("意图写入后中断")
    with monkeypatch.context() as patch:
        patch.setattr(repository, "_recover_cancellation_commit", failed)
        for intent in (_intent(run, record), _intent(other, other_record)):
            with pytest.raises(OSError):
                await repository.commit_cancellation(intent)
    assert await repository.has_pending_cancellation_commit("session", "run")
    assert await repository.has_pending_cancellation_commit("session", "other")
    await repository.recover_cancellation_commit("session", "run")
    await repository.recover_cancellation_commit("session", "run")
    assert not await repository.has_pending_cancellation_commit("session", "run")
    assert await repository.has_pending_cancellation_commit("session", "other")
    assert await repository.load_run("session", "other") == other
    await repository.recover_cancellation_commit("session", "missing")


async def test_pending_success_is_query_only_and_blocks_request(repository) -> None:
    run = _run()
    await repository.create_run(run)
    success = SuccessCommitIntent("conversation", None, RunOutcome.COMPLETED, run.run_id, run.session_id)
    assert not await repository.has_pending_success_commit("session", "run")
    await repository.save_success_commit_intent("session", "run", success)
    assert await repository.has_pending_success_commit("session", "run")
    with pytest.raises(ValueError):
        await repository.save_cancellation("session", _record(), expected_revision=0)


async def test_cancel_request_blocks_success_reservation(repository) -> None:
    run = _run()
    await repository.create_run(run)
    await repository.save_cancellation("session", _record(), expected_revision=0)
    success = SuccessCommitIntent("conversation", None, RunOutcome.COMPLETED, run.run_id, run.session_id)
    with pytest.raises(ValueError, match="取消已保留"):
        await repository.save_success_commit_intent("session", "run", success)
    assert not await repository.has_pending_success_commit("session", "run")


async def test_existing_terminal_event_blocks_cancellation(repository) -> None:
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    await repository.append_event("session", RunEvent("run", 1, RunEventType.RUN_COMPLETED, NOW))
    intent = _intent(run, record)
    intent = replace(intent, events=tuple(replace(event, sequence=event.sequence + 1) for event in intent.events))
    with pytest.raises(ValueError, match="终态事件"):
        await repository.commit_cancellation(intent)


async def test_event_conflict_rejected_before_intent(repository) -> None:
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    await repository.append_event("session", RunEvent("run", 1, RunEventType.RUN_STARTED, NOW))
    with pytest.raises(ValueError):
        await repository.commit_cancellation(_intent(run, record))
    assert (await repository.load_run("session", "run")).state == run.state


async def test_missing_approval_dependency_and_wrong_ownership(repository, tmp_path) -> None:
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    intent = _intent(run, record, ("approval",))
    with pytest.raises(ValueError, match="未注入审批"):
        await repository.commit_cancellation(intent)
    approvals = ApprovalRepositoryAdapter(tmp_path)
    await approvals.create(ApprovalRecord("approval", "other", "session", ApprovalStatus.PENDING, NOW))
    repository._approval_repository = approvals
    with pytest.raises(ValueError, match="审批不属于"):
        await repository.commit_cancellation(intent)
    assert await repository.load_events("session", "run") == ()


@pytest.mark.parametrize("point", ["intent", "event", "approval", "checkpoint", "run", "settled", "delete"])
async def test_file_recovery_after_every_commit_boundary(tmp_path, monkeypatch, point) -> None:
    approvals = ApprovalRepositoryAdapter(tmp_path)
    await approvals.create(ApprovalRecord("approval", "run", "session", ApprovalStatus.PENDING, NOW))
    repository = RunRepositoryAdapter(tmp_path, approval_repository=approvals)
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    directory = tmp_path / "session" / "agent_runs" / "run"
    (directory / "checkpoint.json").write_text("{}", encoding="utf-8")
    intent = _intent(run, record, ("approval",))
    method = {"intent": "_prepare_cancellation_commit_sync", "event": "_ensure_cancellation_events_sync",
              "checkpoint": "_delete_checkpoint_sync", "run": "_save_run_sync",
              "settled": "_settle_cancellation_sync", "delete": "_delete_cancellation_intent_sync"}.get(point)
    if point == "approval":
        original = approvals.consume
        async def interrupted(*args):
            await original(*args)
            raise OSError("模拟审批后中断")
        monkeypatch.setattr(approvals, "consume", interrupted)
    else:
        original = getattr(repository, method)
        def interrupted(*args):
            if point != "delete":
                original(*args)
            raise OSError("模拟文件边界中断")
        monkeypatch.setattr(repository, method, interrupted)
    with pytest.raises(OSError):
        await repository.commit_cancellation(intent)
    assert (directory / "cancel_commit.json").exists()
    # 使用新的适配器模拟重启，不复用故障实例的内存状态。
    recovered = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    await recovered.recover_pending_cancellation_commits()
    await recovered.recover_pending_cancellation_commits()
    assert await recovered.load_run("session", "run") == intent.run
    assert await recovered.load_events("session", "run") == intent.events
    assert await recovered.load_cancellation("session", "run") == intent.settled_record
    assert not (directory / "checkpoint.json").exists()
    assert not (directory / "cancel_commit.json").exists()
    assert (await recovered._approval_repository.load("approval")).status is ApprovalStatus.CONSUMED
    assert not (tmp_path / "session" / "conversation.json").exists()
    assert json.loads((directory / "run.json").read_text(encoding="utf-8"))["version"] == 4
    assert json.loads((directory / "cancellation.json").read_text(encoding="utf-8"))["version"] == 1


async def test_failed_request_write_and_request_survives_restart(tmp_path, monkeypatch) -> None:
    repository = RunRepositoryAdapter(tmp_path)
    await repository.create_run(_run())
    def failed(path, payload):
        raise OSError("磁盘写入失败")
    with monkeypatch.context() as patch:
        patch.setattr(file_module, "write_json_atomic", failed)
        with pytest.raises(OSError):
            await repository.save_cancellation("session", _record(), expected_revision=0)
    assert await repository.load_cancellation("session", "run") is None
    await repository.save_cancellation("session", _record(), expected_revision=0)
    assert await RunRepositoryAdapter(tmp_path).list_pending_cancellations() == (_record(),)


@pytest.mark.parametrize("point", ["partial_temporary_write", "before_replace"])
@pytest.mark.parametrize("line_ending", [b"\r\n", b""])
async def test_cancel_event_batch_failure_preserves_original_file(tmp_path, monkeypatch, point, line_ending) -> None:
    repository = RunRepositoryAdapter(tmp_path)
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    previous = RunEvent("run", 1, RunEventType.RUN_STARTED, NOW)
    await repository.append_event("session", previous)
    path = tmp_path / "session" / "agent_runs" / "run" / "events.jsonl"
    original_bytes = json.dumps(previous.to_dict(), ensure_ascii=False).encode("utf-8") + line_ending
    path.write_bytes(original_bytes)
    intent = _intent(run, record)
    intent = replace(intent, events=tuple(replace(event, sequence=event.sequence + 1) for event in intent.events))
    with monkeypatch.context() as patch:
        if point == "partial_temporary_write":
            original_fdopen = file_module.os.fdopen
            @contextmanager
            def interrupted_fdopen(descriptor, mode, *args, **kwargs):
                with original_fdopen(descriptor, mode, *args, **kwargs) as temporary_file:
                    if mode != "wb":
                        yield temporary_file
                        return
                    def partial_write(payload):
                        temporary_file.write(payload[:len(payload) // 2])
                        temporary_file.flush()
                        raise OSError("临时事件文件写到一半")
                    yield SimpleNamespace(write=partial_write, flush=temporary_file.flush, fileno=temporary_file.fileno)
            patch.setattr(file_module.os, "fdopen", interrupted_fdopen)
        else:
            original_replace = file_module.os.replace
            def interrupted_replace(source, target, *args, **kwargs):
                if Path(target) == path:
                    raise OSError("替换事件文件前中断")
                return original_replace(source, target, *args, **kwargs)
            patch.setattr(file_module.os, "replace", interrupted_replace)
        with pytest.raises(OSError):
            await repository.commit_cancellation(intent)
    assert path.read_bytes() == original_bytes
    assert await repository.load_events("session", "run") == (previous,)
    assert path.with_name("cancel_commit.json").exists()
    assert not tuple(path.parent.glob(".events.cancel.*.tmp"))
    recovered = RunRepositoryAdapter(tmp_path)
    await recovered.recover_pending_cancellation_commits()
    after_recovery = path.read_bytes()
    assert after_recovery.startswith(original_bytes)
    assert await recovered.load_events("session", "run") == (previous,) + intent.events
    assert await recovered.load_run("session", "run") == intent.run
    await recovered.commit_cancellation(intent)
    await recovered.recover_pending_cancellation_commits()
    assert path.read_bytes() == after_recovery


async def test_memory_checkpoint_dependency_is_explicit() -> None:
    repository = InMemoryRunRepository()
    run = replace(_run(), latest_checkpoint_id="checkpoint")
    record = _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    intent = _intent(run, record)
    with pytest.raises(ValueError, match="未注入检查点"):
        await repository.commit_cancellation(intent)
    assert not repository._cancellation_intents
    assert await repository.load_events("session", "run") == ()
    deleted = []
    async def delete(*key):
        deleted.append(key)
    repository._checkpoint_repository = SimpleNamespace(delete=delete)
    await repository.commit_cancellation(intent)
    assert deleted == [("session", "run")]


async def test_memory_recovery_retains_fixed_intent_after_checkpoint_failure() -> None:
    attempts = []
    async def delete(*key):
        attempts.append(key)
        if len(attempts) == 1:
            raise OSError("检查点删除失败")
    repository = InMemoryRunRepository(checkpoint_repository=SimpleNamespace(delete=delete))
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    intent = _intent(run, record)
    with pytest.raises(OSError):
        await repository.commit_cancellation(intent)
    assert repository._cancellation_intents
    assert await repository.load_run("session", "run") == run
    await repository.recover_pending_cancellation_commits()
    await repository.recover_pending_cancellation_commits()
    assert not repository._cancellation_intents
    assert await repository.load_run("session", "run") == intent.run
    assert await repository.load_events("session", "run") == intent.events


async def test_recovery_rejects_modified_existing_event(tmp_path, monkeypatch) -> None:
    repository = RunRepositoryAdapter(tmp_path)
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    intent = _intent(run, record)
    def failed(*args):
        raise OSError("提交终态前中断")
    monkeypatch.setattr(repository, "_save_run_sync", failed)
    with pytest.raises(OSError):
        await repository.commit_cancellation(intent)
    path = tmp_path / "session" / "agent_runs" / "run" / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["data"] = {"冲突": True}
    lines[0] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        await RunRepositoryAdapter(tmp_path).recover_pending_cancellation_commits()
    assert (await repository.load_run("session", "run")).state == run.state


async def test_both_intents_are_rejected_by_success_and_cancel_recovery(tmp_path) -> None:
    repository = RunRepositoryAdapter(tmp_path)
    run, record = _run(), _record()
    await repository.create_run(run)
    await repository.save_cancellation("session", record, expected_revision=0)
    intent = _intent(run, record)
    repository._prepare_cancellation_commit_sync(intent)
    directory = tmp_path / "session" / "agent_runs" / "run"
    (directory / "success_commit.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        await repository.recover_pending_cancellation_commits()
    with pytest.raises(ValueError, match="同时存在"):
        await repository.recover_pending_success_commits()
    assert (directory / "cancel_commit.json").exists()


def test_models_are_frozen_and_validate_request_and_settled() -> None:
    with pytest.raises(FrozenInstanceError):
        _record().revision = 2
    with pytest.raises(ValueError):
        replace(_record().request, reason=" ")
    with pytest.raises(ValueError):
        replace(_record().request, reason="x" * 501)
    with pytest.raises(ValueError):
        replace(_record().request, requested_at="2026-11-06T00:00:00")
    with pytest.raises(ValueError):
        replace(_record(), phase=CancellationPhase.SETTLED)
