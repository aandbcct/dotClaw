"""真实子进程硬终止的工具恢复 pilot。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import json
import os
import statistics
import subprocess
import sys
import time
import traceback
import uuid
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread


def _write_durable(path: Path, value: dict) -> None:
    """原子发布已落盘的独立样本事实，避免观察者读到半写JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: dict) -> None:
    """追加并落盘一条样本记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


async def _prepare_started(root: Path, barrier: Path) -> None:
    """通过真实文件仓储写入 STARTED，再通知父进程。"""
    from dotclaw.common.tool_replay import ToolReplayPolicy, ToolSideEffect
    from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
    from dotclaw.runtime.application.approval_service import ApprovalService
    from dotclaw.runtime.application.dto import ToolRecoveryContract
    from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
    from dotclaw.runtime.domain.control import AgentAction
    from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, MessageRole, RunCheckpoint, RunMessage, RunMessageKind, ToolCall, utc_now_iso
    from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running
    from dotclaw.runtime.domain.tool_recovery import ToolCallKind, ToolCallLedger, ToolCallRecord, ToolCallState
    import dotclaw.runtime.application.tool_recovery as recovery_module

    session_id, run_id, call_id = "pilot-session", "pilot-run", "pilot-call"
    approvals = ApprovalRepositoryAdapter(root)
    runs = RunRepositoryAdapter(root, approval_repository=approvals)
    checkpoints = CheckpointRepositoryAdapter(root)
    recovery = ToolRecoveryService(runs, checkpoints, ApprovalService(approvals))
    run = AgentRun(run_id, session_id, "pilot-agent", AgentRunState(Running(RunStage.EXECUTING_TOOLS)),
        utc_now_iso(), AgentPolicySnapshot("pilot-agent", "pilot-v1", "pilot-model", 3), "pilot-input")
    source = RunMessage("pilot-source", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
        tool_calls=(ToolCall(call_id, "external-effect", {"sample": "pilot"}),))
    checkpoint = RunCheckpoint(f"checkpoint-{run_id}", run_id, session_id, 1, 0, 1,
        {"tool_batch_id": "pilot-source", "pending_call_ids": [call_id]}, {}, AgentAction.EXECUTE_TOOLS,
        tool_ledger_revision=1)
    record = ToolCallRecord(run_id, ToolCall(call_id, "external-effect", {"sample": "pilot"}),
        "pilot-source", 0, ToolCallKind.ORDINARY, ToolCallState.PENDING,
        replay_policy=ToolReplayPolicy.NO_AUTO_RETRY, side_effect=ToolSideEffect.UNKNOWN)
    await runs.create_run(run)
    await runs.save_messages(session_id, run_id, (source,))
    await checkpoints.save(checkpoint)
    await runs.save_tool_ledger(ToolCallLedger(session_id, run_id, 1, (record,)), expected_revision=0)
    await recovery.begin_attempt(run, call_id, checkpoint, ToolRecoveryContract())
    persisted = await runs.load_tool_ledger(session_id, run_id)
    events = await runs.load_events(session_id, run_id)
    if persisted is None or persisted.calls[0].state is not ToolCallState.STARTED or not events:
        raise AssertionError("STARTED 屏障的账本与事件未同时持久化")
    _write_durable(barrier, {"run_id": run_id, "call_id": call_id, "ledger_state": persisted.calls[0].state.value,
        "ledger_revision": persisted.revision, "event_count": len(events), "pid": os.getpid(),
        "source_module": str(Path(recovery_module.__file__).resolve())})
    while True:
        await asyncio.sleep(1)


async def _recover(root: Path, output: Path) -> None:
    """以新进程加载并恢复投影，输出实际持久事实。"""
    from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
    from dotclaw.runtime.application.approval_service import ApprovalService
    from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
    import dotclaw.runtime.application.tool_recovery as recovery_module

    approvals = ApprovalRepositoryAdapter(root)
    runs = RunRepositoryAdapter(root, approval_repository=approvals)
    checkpoints = CheckpointRepositoryAdapter(root)
    recovery = ToolRecoveryService(runs, checkpoints, ApprovalService(approvals))
    begin = time.perf_counter()
    await recovery.recover_projection("pilot-session", "pilot-run")
    duration_ms = (time.perf_counter() - begin) * 1000
    ledger = await runs.load_tool_ledger("pilot-session", "pilot-run")
    events = await runs.load_events("pilot-session", "pilot-run")
    checkpoint = await checkpoints.load("pilot-session", "pilot-run")
    if ledger is None:
        raise AssertionError("重启后账本缺失")
    _write_durable(output, {"pid": os.getpid(), "call_state": ledger.calls[0].state.value,
        "pending_projection": ledger.pending_projection is not None, "event_count": len(events),
        "checkpoint_action": checkpoint.action.value if checkpoint else None, "recovery_ms": duration_ms,
        "source_module": str(Path(recovery_module.__file__).resolve())})


def _child(mode: str, sample: Path) -> None:
    if mode == "prepare":
        asyncio.run(_prepare_started(sample / "store", sample / "barrier.json"))
    else:
        asyncio.run(_recover(sample / "store", sample / "recovery.json"))


def run_pilot(output_root: Path, source_root: Path, timeout_s: float = 30) -> dict:
    """启动真实子进程、外部 kill 屏障进程，再由新进程恢复。"""
    sample = output_root / f"pilot-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    sample.mkdir(parents=True)
    env = os.environ.copy()
    source_path = source_root / "src"
    env["PYTHONPATH"] = str(source_path) + os.pathsep + env.get("PYTHONPATH", "")
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "prepare", "--sample", str(sample)]
    child = subprocess.Popen(command, cwd=source_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    barrier = sample / "barrier.json"
    deadline = time.monotonic() + timeout_s
    while not barrier.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not barrier.exists():
        if child.poll() is None:
            child.kill()
            child.wait(timeout=timeout_s)
        stderr = child.stderr.read().decode("utf-8", errors="replace") if child.poll() is not None and child.stderr else ""
        record = {"sample_id": sample.name, "sample_dir": str(sample), "scenario": "primitive_started_projection_pilot",
            "barrier_reached": False, "child_exit_code": child.poll(), "stderr": stderr}
        _write_durable(sample / "sample.json", record)
        _append_jsonl(output_root / "samples.jsonl", record)
        return record
    barrier_fact = json.loads(barrier.read_text(encoding="utf-8"))
    child.kill()
    child_exit = child.wait(timeout=timeout_s)
    recovery_cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "recover", "--sample", str(sample)]
    recovery_proc = subprocess.run(recovery_cmd, cwd=source_root, env=env, capture_output=True, text=True, timeout=timeout_s)
    recovery_fact = json.loads((sample / "recovery.json").read_text(encoding="utf-8")) if (sample / "recovery.json").exists() else None
    module_path = recovery_fact.get("source_module") if recovery_fact else str(source_path / "dotclaw/runtime/application/tool_recovery.py")
    module_hash = hashlib.sha256(Path(module_path).read_bytes()).hexdigest() if Path(module_path).is_file() else None
    record = {"sample_id": sample.name, "sample_dir": str(sample), "scenario": "primitive_started_projection_pilot", "source_root": str(source_root),
        "source_commit": _git_head(source_root), "source_hash": module_hash,
        "barrier_reached": True, "barrier": barrier_fact, "kill_exit_code": child_exit,
        "recovery_exit_code": recovery_proc.returncode, "recovery": recovery_fact,
        "source_module": module_path,
        "source_tree_match": bool(recovery_fact and Path(recovery_fact["source_module"]).is_relative_to(source_path.resolve()))}
    _write_durable(sample / "sample.json", record)
    _append_jsonl(output_root / "samples.jsonl", record)
    return record


def run_engine_pilot(output_root: Path, source_root: Path, stage: str, policy: str = "no_auto_retry",
                     timeout_s: float = 60, validation_case: str | None = None) -> dict:
    """以真实父进程 kill 和冷启动 Engine 验收单个故障窗。"""
    sample = output_root / f"engine-{stage}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    sample.mkdir(parents=True)
    effects_path = sample / "provider-effects.jsonl"
    server, server_thread = _provider_server(effects_path)
    provider_url = f"http://127.0.0.1:{server.server_port}/effect"
    source_path = source_root / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_path) + os.pathsep + env.get("PYTHONPATH", "")
    env["HARNESS_SOURCE_ROOT"] = str(source_root)
    env["HARNESS_PHASE"] = "initial"
    env["HARNESS_STAGE"] = stage
    env["HARNESS_POLICY"] = policy
    env["HARNESS_VALIDATION_CASE"] = validation_case or ""
    if validation_case == "key_expired":
        expires_at = datetime.now(UTC).timestamp() + 6
        env["HARNESS_KEY_VALID_UNTIL"] = datetime.fromtimestamp(expires_at, UTC).isoformat()
    env["HARNESS_PROVIDER"] = provider_url
    env["HARNESS_RUN_ID"] = f"run-{uuid.uuid4().hex}"
    env["HARNESS_SESSION_ID"] = f"session-{uuid.uuid4().hex}"
    env["HARNESS_CALL_ID"] = f"call-{uuid.uuid4().hex}"
    env["HARNESS_APPROVAL_ID"] = f"approval-{env['HARNESS_CALL_ID']}"
    command = [sys.executable, str(Path(__file__).resolve()), "--child-engine", "start", "--sample", str(sample)]
    initial = subprocess.Popen(command, cwd=source_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    barrier = sample / "engine-barrier.json"
    deadline = time.monotonic() + timeout_s
    approval_pid = None
    cancellation_pid = None
    fault_pid = initial.pid
    fault_process_mode = "start"
    if stage.startswith("approval_"):
        while initial.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        initial_exit = initial.wait(timeout=timeout_s)
        initial_state = json.loads((sample / "start.json").read_text(encoding="utf-8")) if (sample / "start.json").exists() else {}
        env["HARNESS_APPROVAL_ID"] = initial_state.get("approval_id") or env["HARNESS_APPROVAL_ID"]
        env["HARNESS_APPROVED"] = "true" if stage == "approval_approved" else "false"
        env["HARNESS_PHASE"] = "initial"
        approve_cmd = [sys.executable, str(Path(__file__).resolve()), "--child-engine", "approve", "--sample", str(sample)]
        decision_process = subprocess.Popen(approve_cmd, cwd=source_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        approval_pid = decision_process.pid
        deadline = time.monotonic() + timeout_s
        while not barrier.exists() and decision_process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        barrier_fact = json.loads(barrier.read_text(encoding="utf-8")) if barrier.exists() else None
        if decision_process.poll() is None:
            decision_process.kill()
        kill_exit = decision_process.wait(timeout=timeout_s)
    elif stage == "delegation_backfill":
        while not barrier.exists() and initial.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if barrier.exists():
            barrier_fact = json.loads(barrier.read_text(encoding="utf-8"))
            fault_process_mode = "start_child_terminal_before_parent_backfill"
            initial.kill()
            kill_exit = initial.wait(timeout=timeout_s)
            initial_exit = None
        else:
            initial_exit = initial.wait(timeout=timeout_s)
            start_state = json.loads((sample / "start.json").read_text(encoding="utf-8")) if (sample / "start.json").exists() else {}
            if start_state.get("run_state", {}).get("mode", {}).get("type") == "suspended":
                resume_command = [sys.executable, str(Path(__file__).resolve()), "--child-engine",
                    "recover_delegation", "--sample", str(sample)]
                fault_process_mode = "resume_child_terminal_before_parent_backfill"
                env["HARNESS_PHASE"] = "initial"
                fault_process = subprocess.Popen(resume_command, cwd=source_root, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                fault_pid = fault_process.pid
                deadline = time.monotonic() + timeout_s
                while not barrier.exists() and fault_process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                barrier_fact = json.loads(barrier.read_text(encoding="utf-8")) if barrier.exists() else None
                if fault_process.poll() is None:
                    fault_process.kill()
                kill_exit = fault_process.wait(timeout=timeout_s)
            else:
                barrier_fact = None
                kill_exit = initial_exit
    else:
        while not barrier.exists() and initial.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        barrier_fact = json.loads(barrier.read_text(encoding="utf-8")) if barrier.exists() else None
        if initial.poll() is None:
            initial.kill()
        kill_exit = initial.wait(timeout=timeout_s)
        initial_exit = None
    effects_before = _read_effects(effects_path)
    requests_before = _read_effects(sample / "provider-requests.jsonl")
    if stage == "cancel_commit":
        env["HARNESS_PHASE"] = "initial"
        cancel_cmd = [sys.executable, str(Path(__file__).resolve()), "--child-engine", "cancel", "--sample", str(sample)]
        cancellation_process = subprocess.Popen(cancel_cmd, cwd=source_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cancellation_pid = cancellation_process.pid
        cancel_barrier = sample / "cancel-barrier.json"
        deadline = time.monotonic() + timeout_s
        while not cancel_barrier.exists() and cancellation_process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel_fact = json.loads(cancel_barrier.read_text(encoding="utf-8")) if cancel_barrier.exists() else None
        if cancellation_process.poll() is None:
            cancellation_process.kill()
        cancel_kill_exit = cancellation_process.wait(timeout=timeout_s)
    else:
        cancel_kill_exit = None
        cancel_fact = None
    if validation_case == "key_expired":
        deadline_epoch = datetime.fromisoformat(env["HARNESS_KEY_VALID_UNTIL"]).timestamp()
        time.sleep(max(0, deadline_epoch - datetime.now(UTC).timestamp()) + 0.1)
    persisted = _engine_persisted_evidence(sample / "store", env["HARNESS_SESSION_ID"], env["HARNESS_RUN_ID"], stage, env["HARNESS_APPROVAL_ID"])
    confirmed_snapshot = _result_snapshot(sample / "store", env["HARNESS_SESSION_ID"], env["HARNESS_RUN_ID"])
    env["HARNESS_PHASE"] = "recover"
    recovery_started_at = datetime.now(UTC).isoformat()
    recovery_mode = "recover_cancel" if stage == "cancel_commit" else "recover_delegation" if stage == "delegation_backfill" else "recover"
    recover_cmd = [sys.executable, str(Path(__file__).resolve()), "--child-engine", recovery_mode, "--sample", str(sample)]
    resumed = subprocess.run(recover_cmd, cwd=source_root, env=env, capture_output=True, text=True, timeout=timeout_s)
    recovery = json.loads((sample / "recover.json").read_text(encoding="utf-8")) if (sample / "recover.json").exists() else None
    effects_after = _read_effects(effects_path)
    module_path = (recovery or {}).get("engine_module") or (barrier_fact or {}).get("engine_module")
    source_modules = (recovery or {}).get("source_modules") or {"engine": module_path}
    module_hashes = {key: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for key, path in source_modules.items() if path and Path(path).is_file()}
    recovery_result = recovery or {}
    outcome = recovery_result.get("run_outcome")
    ledger_states = recovery_result.get("ledger_states") or []
    persisted_mode = (recovery_result.get("run_state") or {}).get("mode", {})
    key_expired_before_recovery = bool(validation_case == "key_expired"
        and datetime.fromisoformat(env["HARNESS_KEY_VALID_UNTIL"]) < datetime.fromisoformat(recovery_started_at))
    retry_block = _retry_block_result(validation_case, recovery_result, effects_before, effects_after,
        requests_before, _read_effects(sample / "provider-requests.jsonl"))
    if validation_case == "key_expired":
        retry_block = bool(retry_block and key_expired_before_recovery)
    record = {"sample_id": sample.name, "sample_dir": str(sample), "scenario": stage, "policy": policy,
        "validation_case": validation_case, "key_valid_until": env.get("HARNESS_KEY_VALID_UNTIL"),
        "recovery_started_at": recovery_started_at,
        "key_expired_before_recovery": key_expired_before_recovery,
        "version": _source_version(source_root),
        "source_root": str(source_root), "source_commit": _git_head(source_root), "source_hashes": module_hashes,
        "harness_sha256": _harness_hash(),
        "run_id": env["HARNESS_RUN_ID"], "call_id": env["HARNESS_CALL_ID"], "barrier_reached": barrier_fact is not None,
        "barrier": barrier_fact, "persisted_evidence": persisted, "initial_pid": initial.pid,
        "initial_exit_code": initial_exit, "fault_process_pid": fault_pid,
        "fault_process_mode": fault_process_mode, "approval_pid": approval_pid,
        "cancellation_pid": cancellation_pid, "cancellation_kill_exit_code": cancel_kill_exit,
        "cancellation_barrier": cancel_fact,
        "initial_kill_exit_code": kill_exit, "recover_exit_code": resumed.returncode, "recovery": recovery,
        "recovery_duration_ms": recovery_result.get("engine_duration_ms"),
        "task_completed": outcome == "completed",
        "safe_recovery": _safe_recovery_result(stage, policy, outcome, effects_after,
            effects_before, recovery_result, requests_before, _read_effects(sample / "provider-requests.jsonl"),
            confirmed_snapshot, _result_snapshot(sample / "store", env["HARNESS_SESSION_ID"], env["HARNESS_RUN_ID"])),
        "correct_block": _correct_block_result(recovery_result, effects_before, effects_after,
            requests_before, _read_effects(sample / "provider-requests.jsonl")),
        "retry_block": retry_block,
        "expected_error_code": _expected_retry_error(validation_case),
        "confirmed_result_reuse": _confirmed_result_reuse(stage, recovery, requests_before,
            _read_effects(sample / "provider-requests.jsonl"), confirmed_snapshot,
            _result_snapshot(sample / "store", env["HARNESS_SESSION_ID"], env["HARNESS_RUN_ID"])),
        "final_persistent_consistency": _final_persistent_consistency(sample / "store", env["HARNESS_SESSION_ID"],
            env["HARNESS_RUN_ID"], recovery),
        "provider_effects_before": effects_before, "provider_effects_after": effects_after,
        "provider_requests_before": requests_before,
        "provider_requests_after": _read_effects(sample / "provider-requests.jsonl"),
        "duplicate_effects": max(0, len(effects_after) - len({item.get("call_id") for item in effects_after})),
        "unsafe_replay": policy == "no_auto_retry" and len(effects_after) > len({item.get("call_id") for item in effects_after}),
        "unsafe_retry": validation_case in {"safe_budget_zero", "key_expired", "contract_version_changed", "execution_scope_changed"}
            and (len(_read_effects(sample / "provider-requests.jsonl")) > len(requests_before)
                or len(effects_after) > len(effects_before)),
        "provider_effects_path": str(effects_path),
        "engine_module": module_path,
        "source_tree_match": bool(module_path and Path(module_path).resolve().is_relative_to(source_path.resolve())),
        "recover_stderr": resumed.stderr[-4000:] if resumed.returncode else ""}
    _write_durable(sample / "sample.json", record)
    _append_jsonl(output_root / "engine-samples.jsonl", record)
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)
    return record


def _source_version(root: Path) -> str:
    """按源码身份标记版本，不能从故障时恰好存在的账本字段推断。"""
    commit = _git_head(root)
    return "baseline" if commit.startswith("61ad81f7ccad310464d736c2dad39ed8c34a9d35") else "current"


def _harness_hash() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def _safe_recovery_result(stage: str, policy: str, outcome: str | None, effects_after: list[dict],
                          effects_before: list[dict], recovery: dict, requests_before: list[dict], requests_after: list[dict],
                          snapshot_before: str | None, snapshot_after: str | None) -> bool | None:
    if policy == "no_auto_retry":
        if stage in {"messages", "events", "checkpoint", "run", "ledger_result", "intent_clear"}:
            return _confirmed_result_reuse(stage, recovery, requests_before, requests_after,
                snapshot_before, snapshot_after)
        if stage == "approval_approved":
            return outcome == "completed" and len(effects_after) - len(effects_before) == 1
        if stage == "delegation_backfill":
            return outcome == "completed"
        if stage == "approval_rejected":
            mode = (recovery.get("run_state") or {}).get("mode", {})
            return (outcome == "cancelled" and mode.get("type") == "ended"
                and mode.get("outcome") == "cancelled" and not effects_after and not requests_after)
        if stage == "cancel_commit":
            mode = (recovery.get("run_state") or {}).get("mode", {})
            return (mode.get("type") == "ended" and mode.get("outcome") == "cancelled"
                and len(effects_after) == len(effects_before) and len(requests_after) == len(requests_before))
        return None
    if policy == "safe_retry":
        return outcome == "completed" and not effects_after
    if policy == "keyed_idempotent":
        keys = {item.get("idempotency_key") for item in effects_after}
        return outcome == "completed" and len(effects_after) == 1 and len(keys) == 1 and None not in keys
    return None


def _correct_block_result(recovery: dict | None, effects_before: list[dict], effects_after: list[dict],
                          requests_before: list[dict], requests_after: list[dict]) -> bool:
    if not recovery:
        return False
    run_mode = (recovery.get("run_state") or {}).get("mode", {})
    states = recovery.get("ledger_states") or []
    return (recovery.get("run_outcome") == "failed" and run_mode.get("type") == "ended"
        and recovery.get("error_code") == "tool_outcome_uncertain" and "uncertain" in states
        and len(requests_before) == len(requests_after) and len(effects_before) == len(effects_after))


def _expected_retry_error(validation_case: str | None) -> str | None:
    if validation_case == "safe_budget_zero":
        return "tool_recovery_retry_exhausted"
    if validation_case in {"key_expired", "contract_version_changed", "execution_scope_changed"}:
        return "tool_outcome_uncertain"
    return None


def _expected_behavior_for(case_id: str, version: str, default: str) -> str:
    if version == "baseline" and case_id in {"no_auto_before_effect", "no_auto_after_effect"}:
        return "task_completed"
    return default


def _retry_block_result(validation_case: str | None, recovery: dict, effects_before: list[dict],
                        effects_after: list[dict], requests_before: list[dict],
                        requests_after: list[dict]) -> bool | None:
    expected_error = _expected_retry_error(validation_case)
    if expected_error is None:
        return None
    mode = (recovery.get("run_state") or {}).get("mode", {})
    state_ok = (recovery.get("run_outcome") == "failed" and mode.get("type") == "ended"
        and recovery.get("error_code") == expected_error)
    if expected_error == "tool_outcome_uncertain":
        state_ok = state_ok and "uncertain" in (recovery.get("ledger_states") or [])
    return bool(state_ok and len(effects_before) == len(effects_after)
        and len(requests_before) == len(requests_after))


def _result_snapshot(store: Path, session_id: str, run_id: str) -> str | None:
    """摘要已落盘的工具结果及对应消息，供恢复前后独立对照。"""
    run_dir = store / session_id / "agent_runs" / run_id
    ledger_path = run_dir / "tool_calls.json"
    messages_path = run_dir / "messages.json"
    if not messages_path.is_file():
        return None
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {}
    messages_doc = json.loads(messages_path.read_text(encoding="utf-8"))
    messages = messages_doc.get("messages", messages_doc) if isinstance(messages_doc, dict) else messages_doc
    ledger_results = [{"call_id": item.get("call", {}).get("call_id", item.get("call_id")),
        "result": item.get("result")} for item in ledger.get("calls", []) if item.get("result") is not None]
    message_results = [{key: item.get(key) for key in ("message_id", "id", "kind", "tool_call_id", "call_id", "content")
        if key in item} for item in messages if isinstance(item, dict)
        and str(item.get("kind", "")).lower() in {"tool_result", "toolresult"}]
    if ledger_results:
        facts = {"ledger_results": ledger_results}
    elif message_results:
        facts = {"message_results": message_results}
    else:
        return None
    encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _confirmed_result_reuse(stage: str, recovery: dict | None, requests_before: list[dict],
                            requests_after: list[dict], snapshot_before: str | None,
                            snapshot_after: str | None) -> bool | None:
    if stage not in {"messages", "events", "checkpoint", "run", "ledger_result", "intent_clear"}:
        return None
    return bool(recovery and recovery.get("run_outcome") == "completed" and snapshot_before
        and snapshot_before == snapshot_after and len(requests_before) == len(requests_after))


def _final_persistent_consistency(store: Path, session_id: str, run_id: str,
                                  recovery: dict | None) -> bool:
    """独立复读持久文件，核对恢复终态与结果引用，缺少恢复事实一律失败。"""
    if recovery is None:
        return False
    run_dir = store / session_id / "agent_runs" / run_id
    required = [run_dir / "events.jsonl", run_dir / "messages.json", run_dir / "run.json"]
    if not all(path.is_file() for path in required):
        return False
    try:
        events = [json.loads(line) for line in required[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        messages_doc = json.loads(required[1].read_text(encoding="utf-8"))
        run = json.loads(required[2].read_text(encoding="utf-8"))
        ledger_path = run_dir / "tool_calls.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else None
        checkpoint_path = run_dir / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else None
    except (OSError, ValueError, TypeError):
        return False
    if not events:
        return False
    event_seq = [item.get("sequence") for item in events]
    if any(not isinstance(value, int) for value in event_seq):
        return False
    if event_seq[0] != 1 or event_seq != list(range(1, len(event_seq) + 1)):
        return False
    event_ids = [item.get("event_id", item.get("id")) for item in events]
    event_ids = [value for value in event_ids if value is not None]
    if len(event_ids) != len(set(event_ids)):
        return False
    messages = messages_doc.get("messages", messages_doc) if isinstance(messages_doc, dict) else messages_doc
    if not isinstance(messages, list):
        return False
    message_ids = [item.get("message_id", item.get("id")) for item in messages if isinstance(item, dict)]
    if len(message_ids) != len(messages) or any(value is None for value in message_ids):
        return False
    if len(message_ids) != len(set(message_ids)):
        return False
    if recovery.get("run_outcome") == "completed":
        final_id = run.get("final_message_id") or run.get("state", {}).get("final_message_id")
        if final_id and final_id not in message_ids:
            return False
    if run.get("state", {}).get("mode") != (recovery.get("run_state") or {}).get("mode"):
        return False
    if checkpoint and checkpoint.get("action") in {"execute_tools", "execute-tools"}:
        return False
    if any(path.name.endswith("intent.json") or path.name in {"cancel_commit.json", "success_commit.json"}
           for path in run_dir.glob("*.json")):
        return False
    if ledger and ledger.get("pending_projection") is not None:
        return False
    if ledger:
        call_ids = {item.get("call", {}).get("call_id", item.get("call_id")) for item in ledger.get("calls", [])}
        result_refs = []
        for message in messages:
            if isinstance(message, dict) and message.get("kind") == "tool_result":
                linked = message.get("tool_call_id") or message.get("call_id")
                if linked and linked not in call_ids:
                    return False
                if linked:
                    result_refs.append(linked)
        if len(result_refs) != len(set(result_refs)):
            return False
        terminal_calls = [item for item in ledger.get("calls", [])
            if item.get("state") in {"succeeded", "failed", "uncertain", "rejected", "cancelled"}]
        if recovery.get("run_outcome") == "completed" and any(item.get("state") != "succeeded" for item in terminal_calls):
            return False
    return True


def run_performance(output_root: Path, current_root: Path, baseline_root: Path, repetitions: int = 100, warmups: int = 5) -> dict:
    """交替驱动两个版本的串行 RuntimeEngine 工具样本，并计算延迟与写盘量。"""
    sample_root = output_root / f"performance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    sample_root.mkdir(parents=True)
    provider, provider_thread = _provider_server(sample_root / "provider-effects.jsonl")
    workers = {}
    try:
        for label, source_root in (("current", current_root), ("baseline", baseline_root)):
            worker_root = sample_root / label
            worker_root.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(source_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
            env.update({"HARNESS_SOURCE_ROOT": str(source_root), "HARNESS_PROVIDER": f"http://127.0.0.1:{provider.server_port}/effect",
                "HARNESS_PHASE": "performance", "HARNESS_STAGE": "performance", "HARNESS_POLICY": "no_auto_retry"})
            command = [sys.executable, str(Path(__file__).resolve()), "--perf-worker", "--sample", str(worker_root)]
            workers[label] = subprocess.Popen(command, cwd=source_root, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        records = []
        for attempt in range(warmups + repetitions):
            labels = ("current", "baseline") if attempt % 2 == 0 else ("baseline", "current")
            for label in labels:
                request = {"sample_id": f"{label}-{attempt:03d}-{uuid.uuid4().hex[:8]}",
                    "run_id": f"run-{uuid.uuid4().hex}", "session_id": f"session-{uuid.uuid4().hex}",
                    "call_id": f"call-{uuid.uuid4().hex}", "is_warmup": attempt < warmups}
                worker = workers[label]
                worker.stdin.write(json.dumps(request) + "\n")
                worker.stdin.flush()
                line = worker.stdout.readline()
                if not line:
                    raise RuntimeError(f"{label} performance worker exited unexpectedly: {worker.stderr.read()[-2000:]}")
                record = json.loads(line)
                record["version"] = label
                record["is_warmup"] = request["is_warmup"]
                records.append(record)
                _append_jsonl(sample_root / "samples.jsonl", record)
        formal = {label: [item for item in records if item["version"] == label and not item["is_warmup"]]
            for label in ("current", "baseline")}
        stats = {label: {"count": len(items), "p50_ms": statistics.median(item["engine_duration_ms"] for item in items),
            "p95_ms": _percentile([item["engine_duration_ms"] for item in items], 0.95),
            "mean_write_bytes": statistics.mean(item["write_bytes"] for item in items)} for label, items in formal.items()}
        relative = {}
        for metric in ("p50_ms", "p95_ms", "mean_write_bytes"):
            base = stats["baseline"][metric]
            relative[metric] = (stats["current"][metric] - base) / base * 100 if base else None
        summary = {"sample_root": str(sample_root), "source_commits": {"current": _git_head(current_root),
            "baseline": _git_head(baseline_root)}, "warmups_per_version": warmups,
            "formal_samples_per_version": repetitions, "order": "alternating pair order",
            "statistics": stats, "relative_difference_percent": relative}
        _write_durable(sample_root / "summary.json", summary)
        return summary
    finally:
        for worker in workers.values():
            if worker.stdin and not worker.stdin.closed:
                worker.stdin.close()
            worker.wait(timeout=30)
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.5)))]


def _performance_worker(sample_root: Path) -> None:
    """保持子进程存活，逐个执行父进程交替调度的独立输入。"""
    for line in sys.stdin:
        request = json.loads(line)
        sample = sample_root / request["sample_id"]
        sample.mkdir()
        os.environ.update({"HARNESS_RUN_ID": request["run_id"], "HARNESS_SESSION_ID": request["session_id"],
            "HARNESS_CALL_ID": request["call_id"]})
        asyncio.run(_run_engine_child("perf_run", sample))
        result = json.loads((sample / "perf_run.json").read_text(encoding="utf-8"))
        result.update({"sample_id": request["sample_id"], "sample_dir": str(sample),
            "source_root": os.environ["HARNESS_SOURCE_ROOT"], "source_commit": _git_head(Path(os.environ["HARNESS_SOURCE_ROOT"])),
            "write_bytes": sum(path.stat().st_size for path in (sample / "store").rglob("*") if path.is_file())})
        print(json.dumps(result, ensure_ascii=False), flush=True)


def _engine_persisted_evidence(store: Path, session_id: str, run_id: str, stage: str, approval_id: str) -> dict:
    """从样本文件独立确认工具启动屏障已达到。"""
    run_dir = store / session_id / "agent_runs" / run_id
    events_path = run_dir / "events.jsonl"
    checkpoint_path = run_dir / "checkpoint.json"
    ledger_path = run_dir / "tool_calls.json"
    messages_path = run_dir / "messages.json"
    run_path = run_dir / "run.json"
    raw_events = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else None
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else None
    raw_messages = messages_path.read_text(encoding="utf-8").lower() if messages_path.is_file() else ""
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else None
    child_association_path = store.parent / "child-association.json"
    child_association = json.loads(child_association_path.read_text(encoding="utf-8")) if child_association_path.is_file() else None
    approval_path = store / "approvals" / f"{approval_id}.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8")) if approval_path.is_file() else None
    cancellation_path = run_dir / "cancel_commit.json"
    started_event = "tool_started" in raw_events.lower()
    started_ledger = bool(ledger and any(item.get("state") == "started" for item in ledger.get("calls", [])))
    result_message = '"tool_result"' in raw_messages
    completed_event = "tool_completed" in raw_events.lower()
    result_ledger = bool(ledger and any(item.get("result") is not None for item in ledger.get("calls", [])))
    action = checkpoint.get("action") if checkpoint else None
    if stage in {"before_effect", "after_effect"}:
        target_fact = started_event and action == "execute_tools" and (started_ledger or not ledger_path.exists())
    elif stage == "messages":
        target_fact = result_message
    elif stage == "events":
        target_fact = completed_event
    elif stage == "checkpoint":
        target_fact = result_message and action == "invoke_llm"
    elif stage == "run":
        target_fact = bool(run and run.get("statistics", {}).get("tool_call_count", 0) > 0)
    elif stage == "ledger_result":
        target_fact = result_ledger and ledger.get("pending_projection") is not None
    elif stage in {"approval_approved", "approval_rejected"}:
        expected = stage == "approval_approved"
        decisions = [item.get("approval_decision") for item in ledger.get("calls", [])] if ledger else []
        target_fact = expected in decisions or bool(approval and approval.get("status") == "consumed")
    elif stage == "cancel_commit":
        target_fact = cancellation_path.is_file()
    elif stage == "delegation_backfill":
        child_run_dir = (store / child_association["child_session_id"] / "agent_runs" / child_association["child_run_id"]
            if child_association else None)
        child_run = json.loads((child_run_dir / "run.json").read_text(encoding="utf-8")) if child_run_dir and (child_run_dir / "run.json").is_file() else None
        child_terminal = bool(child_run and child_run.get("state", {}).get("mode", {}).get("type") == "ended")
        child_messages_path = child_run_dir / "messages.json" if child_run_dir else None
        child_messages_doc = json.loads(child_messages_path.read_text(encoding="utf-8")) if child_messages_path and child_messages_path.is_file() else {}
        child_messages = child_messages_doc.get("messages", child_messages_doc) if isinstance(child_messages_doc, dict) else child_messages_doc
        child_input = next((message.get("content") for message in child_messages if isinstance(message, dict)
            and message.get("role") == "user"), None)
        delegation_fixture_match = bool(child_run and child_association
            and child_run.get("run_id") == child_association["child_run_id"]
            and child_run.get("parent_run_id") == child_association["parent_run_id"]
            and child_run.get("root_run_id") == child_association["root_run_id"]
            and child_run.get("delegation_task_id") == child_association["task_id"]
            and child_run.get("agent_id") == child_association["target_agent_id"]
            and child_input == child_association["input_message"])
        target_fact = bool(child_terminal and delegation_fixture_match and run
            and run.get("state", {}).get("mode", {}).get("type") == "suspended")
    else:
        target_fact = result_ledger and ledger.get("pending_projection") is None
    return {"run_dir_exists": run_dir.is_dir(), "tool_started_event": started_event,
        "checkpoint_action": action,
        "ledger_started": started_ledger, "tool_result_message": result_message,
        "tool_completed_event": completed_event, "ledger_result": result_ledger,
        "approval_status": approval.get("status") if approval else None,
        "cancel_commit_intent": cancellation_path.is_file(),
        "child_run_id": child_association.get("child_run_id") if child_association else None,
        "child_terminal": child_terminal if stage == "delegation_backfill" else None,
        "delegation_fixture_match": delegation_fixture_match if stage == "delegation_backfill" else None,
        "approval_decision": next((value for value in (ledger or {}).get("calls", [])
            if value.get("approval_decision") is not None), {}).get("approval_decision") if ledger else None,
        "target_fact_present": target_fact, "events_path": str(events_path),
        "result_snapshot_before": _result_snapshot(store, session_id, run_id),
        "barrier_durable": target_fact}


def _read_effects(path: Path) -> list[dict]:
    """独立读取父进程提供方已 fsync 的效果事实。"""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _git_head(root: Path) -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _provider_server(effects_path: Path) -> tuple[http.server.ThreadingHTTPServer, Thread]:
    """运行父进程持有的独立效果计数服务。"""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = request.get("idempotency_key")
            existing = _read_effects(effects_path) if key else []
            effect = next((item for item in existing if key and item.get("idempotency_key") == key), None)
            created = effect is None and request.get("policy") != "safe_retry"
            if effect is None:
                effect = {"effect_id": uuid.uuid4().hex, "idempotency_key": key, **request}
                if created:
                    _append_jsonl(effects_path, effect)
            request_fact = {"request_id": uuid.uuid4().hex, "idempotency_key": key,
                "created_effect": created, "policy": request.get("policy"), "call_id": request["call_id"]}
            _append_jsonl(effects_path.with_name("provider-requests.jsonl"), request_fact)
            effect = {**effect, "created_effect": created}
            payload = json.dumps(effect).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _PersistenceBarrier:
    """包装真实文件仓储，在可读写事实落盘后停住指定实验进程。"""

    def __init__(self, target, sample: Path, stage: str, phase: str) -> None:
        self._target = target
        self._sample = sample
        self._stage = stage
        self._phase = phase
        self._result_fact_seen = False

    def __getattr__(self, name: str):
        method = getattr(self._target, name)
        supported = {"save_tool_ledger", "ensure_tool_messages", "save_messages", "ensure_tool_events",
            "append_event", "ensure_tool_checkpoint", "ensure_tool_run", "save_run", "commit_success", "save", "consume"}
        if name not in supported:
            return method

        async def persist_then_wait(*args, **kwargs):
            result = await method(*args, **kwargs)
            if (self._stage == "run" and name == "save_tool_ledger" and args[0].pending_projection is not None
                    and any(call.result is not None for call in args[0].calls)):
                self._result_fact_seen = True
            if self._phase == "initial" and self._matches(name, args, result):
                persisted_run = args[0] if name == "save_run" and args else None
                evidence = {"method": name, "stage": self._stage, "pid": os.getpid(),
                    "call_id": os.environ["HARNESS_CALL_ID"],
                    "run_id": getattr(persisted_run, "run_id", os.environ["HARNESS_RUN_ID"]),
                    "child_run_id": getattr(persisted_run, "run_id", None),
                    "engine_module": os.environ["HARNESS_ENGINE_MODULE"]}
                _write_durable(self._sample / "engine-barrier.json", evidence)
                while True:
                    await asyncio.sleep(1)
            return result


        return persist_then_wait

        return persist_then_wait

    def _matches(self, method: str, args: tuple, result) -> bool:
        if self._stage in {"approval_approved", "approval_rejected"} and method == "save_tool_ledger":
            approved = self._stage == "approval_approved"
            return any(call.approval_decision is approved for call in args[0].calls)
        if self._stage.startswith("approval_") and method == "consume":
            return result is not None
        if self._stage == "ledger_result" and method == "save_tool_ledger":
            ledger = args[0]
            return ledger.pending_projection is not None and any(call.result is not None for call in ledger.calls)
        if self._stage == "intent_clear" and method == "save_tool_ledger":
            ledger = args[0]
            return ledger.pending_projection is None and any(call.result is not None for call in ledger.calls)
        if self._stage == "messages" and method in {"ensure_tool_messages", "save_messages"}:
            messages = args[-1]
            return any(getattr(message.kind, "value", message.kind) in {"tool_result", "delegation_result"} for message in messages)
        if self._stage == "events" and method in {"ensure_tool_events", "append_event"}:
            events = (args[-1],) if method == "append_event" else args[-1]
            return any(getattr(event.event_type, "value", event.event_type) == "tool_completed" for event in events)
        if self._stage == "checkpoint" and method in {"ensure_tool_checkpoint", "save"}:
            checkpoint = args[-1] if method == "ensure_tool_checkpoint" else args[0]
            if checkpoint is None or getattr(checkpoint.action, "value", checkpoint.action) != "invoke_llm":
                return False
            messages_path = self._sample / "store" / checkpoint.session_id / "agent_runs" / checkpoint.run_id / "messages.json"
            return messages_path.is_file() and '"tool_result"' in messages_path.read_text(encoding="utf-8").lower()
        if self._stage == "run" and method == "ensure_tool_run":
            return self._result_fact_seen
        if self._stage == "run" and method == "save_run":
            return getattr(args[0].statistics, "tool_call_count", 0) > 0
        if self._stage == "delegation_backfill" and method == "save_run":
            return args[0].run_id != os.environ["HARNESS_RUN_ID"] and args[0].state.is_ended()
        if self._stage == "delegation_backfill" and method == "commit_success":
            return args[0].run_id != os.environ["HARNESS_RUN_ID"] and args[0].state.is_ended()
        return False


FORMAL_CASES = (
    ("no_auto_before_effect", "before_effect", "no_auto_retry", "correct_block"),
    ("no_auto_after_effect", "after_effect", "no_auto_retry", "correct_block"),
    ("messages_projection", "messages", "no_auto_retry", "safe_recovery"),
    ("events_projection", "events", "no_auto_retry", "safe_recovery"),
    ("checkpoint_projection", "checkpoint", "no_auto_retry", "safe_recovery"),
    ("run_projection", "run", "no_auto_retry", "safe_recovery"),
    ("ledger_result_projection", "ledger_result", "no_auto_retry", "safe_recovery"),
    ("intent_clear_projection", "intent_clear", "no_auto_retry", "safe_recovery"),
    ("approval_approved", "approval_approved", "no_auto_retry", "safe_recovery"),
    ("approval_rejected", "approval_rejected", "no_auto_retry", "safe_recovery"),
    ("cancel_commit", "cancel_commit", "no_auto_retry", "safe_recovery"),
    ("delegation_backfill", "delegation_backfill", "no_auto_retry", "safe_recovery"),
    ("safe_retry_before_effect", "before_effect", "safe_retry", "safe_recovery"),
    ("safe_retry_after_effect", "after_effect", "safe_retry", "safe_recovery"),
    ("keyed_before_effect", "before_effect", "keyed_idempotent", "safe_recovery"),
    ("keyed_after_effect", "after_effect", "keyed_idempotent", "safe_recovery"),
    ("safe_budget_zero", "after_effect", "safe_retry", "retry_block"),
    ("key_expired", "after_effect", "keyed_idempotent", "retry_block"),
    ("contract_version_changed", "after_effect", "keyed_idempotent", "retry_block"),
    ("execution_scope_changed", "after_effect", "keyed_idempotent", "retry_block"),
)

FORMAL_VERSION_SCOPE = {
    "ledger_result_projection": ("current",),
    "intent_clear_projection": ("current",),
    "approval_approved": ("current",),
    "approval_rejected": ("current",),
    "safe_retry_before_effect": ("current",),
    "safe_retry_after_effect": ("current",),
    "keyed_before_effect": ("current",),
    "keyed_after_effect": ("current",),
    "safe_budget_zero": ("current",),
    "key_expired": ("current",),
    "contract_version_changed": ("current",),
    "execution_scope_changed": ("current",),
}


def run_formal(output_root: Path, roots: dict[str, Path], repetitions: int = 30) -> dict:
    """按固定预注册映射串行运行样本，所有失败和超时均写入独立目录及 JSONL。"""
    batch = output_root
    batch_id = batch.name
    batch.mkdir(parents=True, exist_ok=False)
    mapping = [{"case_id": case_id, "stage": stage, "policy": policy,
        "expected_behavior": expected,
        "expected_behavior_by_version": {version: _expected_behavior_for(case_id, version, expected)
            for version in FORMAL_VERSION_SCOPE.get(case_id, roots)},
        "versions": list(FORMAL_VERSION_SCOPE.get(case_id, roots)),
        "not_comparable_versions": sorted(set(roots) - set(FORMAL_VERSION_SCOPE.get(case_id, roots))),
        "repetitions_per_version": repetitions}
        for case_id, stage, policy, expected in FORMAL_CASES]
    _write_durable(batch / "suite.json", {"batch_id": batch_id, "cases": mapping,
        "source_commits": {version: _git_head(root) for version, root in roots.items()},
        "harness_sha256": _harness_hash(),
        "validity_rule": "barrier missing or import/source mismatch is invalid; any recovery/behavior failure after valid barrier remains in denominator"})
    rows = []
    for case_id, stage, policy, expected in FORMAL_CASES:
        scoped_versions = FORMAL_VERSION_SCOPE.get(case_id, tuple(roots))
        for version in scoped_versions:
            source_root = roots[version]
            version_expected = _expected_behavior_for(case_id, version, expected)
            for repetition in range(1, repetitions + 1):
                label = f"{case_id}-{version}-{repetition:02d}"
                before_samples = {path.name for path in batch.glob("engine-*") if path.is_dir()}
                try:
                    validation_case = case_id if case_id in FORMAL_VERSION_SCOPE and case_id in {
                        "safe_budget_zero", "key_expired", "contract_version_changed", "execution_scope_changed"} else None
                    row = run_engine_pilot(batch, source_root, stage, policy, validation_case=validation_case)
                    row["case_id"] = case_id
                    row["case_repetition"] = repetition
                    row["expected_behavior"] = version_expected
                    row["formal_version"] = version
                    row["harness_sha256"] = _harness_hash()
                    row["valid_sample"] = bool(row.get("barrier_reached") and row.get("source_tree_match"))
                    row["expected_behavior_met"] = bool(row.get(version_expected)) if row["valid_sample"] else None
                except Exception as exc:
                    failed = batch / f"failed-{label}-{uuid.uuid4().hex[:8]}"
                    failed.mkdir()
                    partial_samples = [str(path) for path in batch.glob("engine-*")
                        if path.is_dir() and path.name not in before_samples]
                    row = {"sample_id": failed.name, "sample_dir": str(failed), "case_id": case_id,
                        "case_repetition": repetition, "scenario": stage, "policy": policy,
                        "expected_behavior": version_expected, "formal_version": version,
                        "harness_sha256": _harness_hash(),
                        "source_root": str(source_root), "source_commit": _git_head(source_root),
                        "valid_sample": False, "expected_behavior_met": None,
                        "partial_sample_dirs": partial_samples,
                        "initialization_or_harness_error": f"{type(exc).__name__}: {exc}",
                        "exception_traceback": traceback.format_exc()}
                    _write_durable(failed / "sample.json", row)
                _append_jsonl(batch / "formal-samples.jsonl", row)
                rows.append(row)
    grouped = {}
    for row in rows:
        key = f"{row.get('case_id')}:{row.get('formal_version')}"
        bucket = grouped.setdefault(key, {"valid_denominator": 0, "safe_recovery": 0,
            "correct_block": 0, "confirmed_result_reuse": 0, "persistent_consistency": 0,
            "duplicate_effects": 0, "unsafe_replay": 0, "expected_behavior_met": 0,
            "retry_block": 0, "unsafe_retry": 0,
            "safe_recovery_n": 0, "correct_block_n": 0, "confirmed_result_reuse_n": 0,
            "persistent_consistency_n": 0, "expected_behavior_met_n": 0, "retry_block_n": 0})
        if row.get("valid_sample"):
            bucket["valid_denominator"] += 1
            for metric, field in (("safe_recovery", "safe_recovery"), ("correct_block", "correct_block"),
                    ("confirmed_result_reuse", "confirmed_result_reuse"),
                    ("persistent_consistency", "final_persistent_consistency"),
                    ("expected_behavior_met", "expected_behavior_met"),
                    ("retry_block", "retry_block")):
                if row.get(field) is not None:
                    bucket[metric + "_n"] += 1
                    bucket[metric] += int(row.get(field) is True)
            bucket["duplicate_effects"] += row.get("duplicate_effects", 0)
            bucket["unsafe_replay"] += int(row.get("unsafe_replay", False))
            bucket["unsafe_retry"] += int(row.get("unsafe_retry", False))
    summary = {"batch_id": batch_id, "sample_count": len(rows),
        "valid_count": sum(row.get("valid_sample", False) for row in rows),
        "invalid_count": sum(not row.get("valid_sample", False) for row in rows),
        "case_mapping": mapping, "by_case_version": grouped,
        "jsonl": str(batch / "formal-samples.jsonl")}
    _write_durable(batch / "summary.json", summary)
    return summary


def run_current_preflight(output_root: Path, source_root: Path) -> dict:
    """正式批次前按固定映射逐项执行一次新版屏障与恢复判据预检。"""
    output_root.mkdir(parents=True, exist_ok=False)
    rows = []
    for case_id, stage, policy, expected in FORMAL_CASES:
        sample_root = output_root / case_id
        sample_root.mkdir()
        before = {path.name for path in sample_root.glob("engine-*") if path.is_dir()}
        validation_case = case_id if case_id in {
            "safe_budget_zero", "key_expired", "contract_version_changed", "execution_scope_changed"} else None
        try:
            row = run_engine_pilot(sample_root, source_root, stage, policy, validation_case=validation_case)
            row.update({"case_id": case_id, "expected_behavior": expected, "formal_version": "current",
                "harness_sha256": _harness_hash(),
                "valid_sample": bool(row.get("barrier_reached") and row.get("source_tree_match")),
                "expected_behavior_met": bool(row.get(expected)) if row.get("barrier_reached") else None})
        except Exception as exc:
            partial = [str(path) for path in sample_root.glob("engine-*")
                if path.is_dir() and path.name not in before]
            failure_dir = sample_root / f"failed-{uuid.uuid4().hex[:8]}"
            failure_dir.mkdir()
            row = {"sample_id": failure_dir.name, "sample_dir": str(failure_dir), "case_id": case_id,
                "scenario": stage, "policy": policy, "expected_behavior": expected,
                "formal_version": "current", "harness_sha256": _harness_hash(),
                "valid_sample": False, "expected_behavior_met": None,
                "partial_sample_dirs": partial, "initialization_or_harness_error": f"{type(exc).__name__}: {exc}"}
            _write_durable(failure_dir / "sample.json", row)
        _append_jsonl(output_root / "preflight.jsonl", row)
        rows.append(row)
    summary = {"sample_count": len(rows), "valid_count": sum(row.get("valid_sample", False) for row in rows),
        "expected_behavior_count": sum(row.get("expected_behavior_met") is True for row in rows),
        "harness_sha256": _harness_hash(),
        "failed_cases": [row.get("case_id") for row in rows
            if not row.get("valid_sample") or row.get("expected_behavior_met") is not True],
        "jsonl": str(output_root / "preflight.jsonl")}
    _write_durable(output_root / "summary.json", summary)
    return summary


def run_baseline_projection_rechecks(output_root: Path, source_root: Path, repetitions: int = 30) -> dict:
    """补采旧版投影窗口恢复前消息摘要，修正原批次缺少的基线复用观测。"""
    output_root.mkdir(parents=True, exist_ok=False)
    cases = ("messages", "events", "checkpoint", "run")
    rows = []
    for case_id in cases:
        stage_root = output_root / case_id
        stage_root.mkdir()
        for repetition in range(1, repetitions + 1):
            try:
                row = run_engine_pilot(stage_root, source_root, case_id, "no_auto_retry")
                row.update({"case_id": case_id, "case_repetition": repetition, "formal_version": "baseline",
                    "expected_behavior": "safe_recovery", "harness_sha256": _harness_hash(),
                    "valid_sample": bool(row.get("barrier_reached") and row.get("source_tree_match")),
                    "expected_behavior_met": bool(row.get("safe_recovery"))
                        if row.get("barrier_reached") and row.get("source_tree_match") else None})
            except Exception as exc:
                failed = stage_root / f"failed-{repetition:02d}-{uuid.uuid4().hex[:8]}"
                failed.mkdir()
                row = {"sample_id": failed.name, "sample_dir": str(failed), "case_id": case_id,
                    "case_repetition": repetition, "formal_version": "baseline", "scenario": case_id,
                    "policy": "no_auto_retry", "expected_behavior": "safe_recovery",
                    "harness_sha256": _harness_hash(), "valid_sample": False, "expected_behavior_met": None,
                    "initialization_or_harness_error": f"{type(exc).__name__}: {exc}"}
                _write_durable(failed / "sample.json", row)
            _append_jsonl(output_root / "projection-rechecks.jsonl", row)
            rows.append(row)
    summary = {"sample_count": len(rows), "valid_count": sum(row.get("valid_sample", False) for row in rows),
        "expected_behavior_count": sum(row.get("expected_behavior_met") is True for row in rows),
        "measurement_fix": "baseline has no tool ledger; use persisted tool_result message identity/body snapshots before and after recovery",
        "by_case": {case: {"valid": sum(row.get("case_id") == case and row.get("valid_sample", False) for row in rows),
            "safe_recovery": sum(row.get("case_id") == case and row.get("safe_recovery") is True for row in rows),
            "confirmed_result_reuse": sum(row.get("case_id") == case and row.get("confirmed_result_reuse") is True for row in rows),
            "duplicate_effects": sum(row.get("duplicate_effects", 0) for row in rows if row.get("case_id") == case)}
            for case in cases}, "jsonl": str(output_root / "projection-rechecks.jsonl"),
        "harness_sha256": _harness_hash(), "source_commit": _git_head(source_root)}
    _write_durable(output_root / "summary.json", summary)
    return summary


def derive_formal_summary(formal_root: Path, supplemental_root: Path | None = None,
                          supplemental_cancel_sample: Path | None = None) -> dict:
    """从不可变样本行生成按版本修正预期标签的派生指标，不改写原始JSONL。"""
    rows = [json.loads(line) for line in (formal_root / "formal-samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped: dict[str, dict] = {}
    for row in rows:
        case_id = row.get("case_id")
        version = row.get("formal_version")
        key = f"{case_id}:{version}"
        bucket = grouped.setdefault(key, {"case_id": case_id, "version": version,
            "valid_denominator": 0, "expected_behavior": _expected_behavior_for(case_id, version, row.get("expected_behavior")),
            "expected_behavior_met": 0, "safe_recovery_n": 0, "safe_recovery": 0,
            "correct_block_n": 0, "correct_block": 0, "retry_block_n": 0, "retry_block": 0,
            "confirmed_result_reuse_n": 0, "confirmed_result_reuse": 0,
            "persistent_consistency_n": 0, "persistent_consistency": 0,
            "duplicate_effects": 0, "unsafe_replay": 0, "unsafe_retry": 0,
            "baseline_legacy_completion": 0, "label_reclassified_samples": [],
            "measurement_invalid": {"confirmed_result_reuse": 0, "safe_recovery": 0}})
        if case_id in {"no_auto_before_effect", "no_auto_after_effect"} and version == "baseline":
            bucket["label_reclassified_samples"].append(row.get("sample_id"))
        if not row.get("valid_sample"):
            continue
        bucket["valid_denominator"] += 1
        if version == "baseline" and case_id in {"no_auto_before_effect", "no_auto_after_effect"}:
            behavior_met = row.get("task_completed") is True
            bucket["baseline_legacy_completion"] += int(behavior_met)
        else:
            expected = _expected_behavior_for(case_id, version, row.get("expected_behavior"))
            behavior_met = row.get(expected) is True
        bucket["expected_behavior_met"] += int(behavior_met)
        baseline_projection_without_snapshot = (version == "baseline"
            and case_id in {"messages_projection", "events_projection", "checkpoint_projection", "run_projection"}
            and not (row.get("persisted_evidence") or {}).get("result_snapshot_before"))
        if baseline_projection_without_snapshot:
            bucket["measurement_invalid"]["confirmed_result_reuse"] += 1
            if row.get("task_completed") and not row.get("duplicate_effects"):
                bucket["measurement_invalid"]["safe_recovery"] += 1
            else:
                bucket["safe_recovery_n"] += 1
                bucket["safe_recovery"] += int(row.get("safe_recovery") is True)
        elif bucket["expected_behavior"] == "safe_recovery" and row.get("safe_recovery") is not None:
            bucket["safe_recovery_n"] += 1
            bucket["safe_recovery"] += int(row.get("safe_recovery") is True)
        if version == "current" and case_id in {"no_auto_before_effect", "no_auto_after_effect"}:
            bucket["correct_block_n"] += 1
            bucket["correct_block"] += int(row.get("correct_block") is True)
        if row.get("retry_block") is not None:
            bucket["retry_block_n"] += 1
            bucket["retry_block"] += int(row.get("retry_block") is True)
        if case_id.endswith("_projection") and not baseline_projection_without_snapshot and row.get("confirmed_result_reuse") is not None:
            bucket["confirmed_result_reuse_n"] += 1
            bucket["confirmed_result_reuse"] += int(row.get("confirmed_result_reuse") is True)
        if row.get("final_persistent_consistency") is not None:
            bucket["persistent_consistency_n"] += 1
            bucket["persistent_consistency"] += int(row.get("final_persistent_consistency") is True)
        duplicates = int(row.get("duplicate_effects", 0) or 0)
        bucket["duplicate_effects"] += duplicates
        bucket["unsafe_replay"] += int(version == "baseline" and row.get("policy") == "no_auto_retry" and duplicates > 0)
        bucket["unsafe_retry"] += int(row.get("unsafe_retry", False))
    supplemental_summary = None
    if supplemental_root:
        supplemental_rows = [json.loads(line) for line in (supplemental_root / "projection-rechecks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        for case_id in ("messages", "events", "checkpoint", "run"):
            bucket = grouped[f"{case_id}_projection:baseline"]
            stage_rows = [row for row in supplemental_rows if row.get("case_id") == case_id and row.get("valid_sample")]
            bucket["supplemental_measurement_valid_n"] = len(stage_rows)
            bucket["supplemental_confirmed_result_reuse"] = sum(row.get("confirmed_result_reuse") is True for row in stage_rows)
            bucket["supplemental_safe_recovery"] = sum(row.get("safe_recovery") is True for row in stage_rows)
            bucket["supplemental_duplicate_effects"] = sum(row.get("duplicate_effects", 0) for row in stage_rows)
            bucket["measurement_recheck_jsonl"] = str(supplemental_root / "projection-rechecks.jsonl")
        supplemental_summary = {"root": str(supplemental_root), "sample_count": len(supplemental_rows),
            "valid_count": sum(row.get("valid_sample", False) for row in supplemental_rows)}
    cancel_supplement = None
    if supplemental_cancel_sample:
        row = json.loads(supplemental_cancel_sample.read_text(encoding="utf-8"))
        if (row.get("barrier_reached") and row.get("source_tree_match") and row.get("cancellation_barrier")
                and row.get("cancellation_kill_exit_code") is not None):
            bucket = grouped["cancel_commit:baseline"]
            bucket["supplemental_valid_n"] = 1
            bucket["effective_valid_denominator"] = bucket["valid_denominator"] + 1
            bucket["safe_recovery_n"] += 1
            bucket["safe_recovery"] += int(row.get("safe_recovery") is True)
            bucket["supplemental_expected_behavior_met"] = int(row.get("safe_recovery") is True)
            bucket["supplemental_sample_id"] = row.get("sample_id")
            bucket["supplemental_recovery_exit_code"] = row.get("recover_exit_code")
            bucket["supplemental_recovery_error"] = "取消记录修订号冲突" if "取消记录修订号冲突" in row.get("recover_stderr", "") else None
            bucket["duplicate_effects"] += int(row.get("duplicate_effects", 0) or 0)
            cancel_supplement = {"sample_json": str(supplemental_cancel_sample), "sample_id": row.get("sample_id"),
                "valid_barrier": True, "recovery_exit_code": row.get("recover_exit_code")}
    summary = {"batch_id": formal_root.name, "raw_jsonl": str(formal_root / "formal-samples.jsonl"),
        "raw_jsonl_unchanged": True, "raw_sample_count": len(rows),
        "raw_valid_count": sum(row.get("valid_sample", False) for row in rows),
        "effective_valid_count_with_supplements": sum(row.get("valid_sample", False) for row in rows) + (1 if cancel_supplement else 0),
        "attempted_count_with_supplements": len(rows) + (1 if cancel_supplement else 0),
        "harness_sha256": json.loads((formal_root / "suite.json").read_text(encoding="utf-8"))["harness_sha256"],
        "source_commits": json.loads((formal_root / "suite.json").read_text(encoding="utf-8"))["source_commits"],
        "label_correction": {"baseline_NO_AUTO_expected_behavior": "task_completed for before_effect and after_effect",
            "raw_labels_preserved": True,
            "safety_interpretation": "duplicate effects remain a separate safety failure"},
        "supplemental_measurement": supplemental_summary,
        "supplemental_cancel_sample": cancel_supplement,
        "by_case_version": grouped}
    _write_durable(formal_root / "version-specific-summary.json", summary)
    return summary


async def _run_engine_child(mode: str, sample: Path) -> None:
    """真实装配文件仓储与 RuntimeEngine，初次执行或冷恢复一次工具调用。"""
    from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
    from dotclaw.runtime.application.approval_service import ApprovalService
    from dotclaw.runtime.application.cancellation_service import CancellationService
    from dotclaw.runtime.application.context_budget import TokenCountResult
    from dotclaw.runtime.application.dto import (ContextBundle, ContextMetadata, ConversationMessage,
        ConversationSnapshot, DelegationRequest, DelegationResult, DelegationSubmission, RunRequest,
        ToolInvocation, ToolResult, ToolResultStatus)
    import dotclaw.runtime.application.dto as dto_module
    from dotclaw.runtime.application.engine import RuntimeEngine
    from dotclaw.runtime.application.execution import RunExecutionView
    from dotclaw.runtime.application.history_compaction import HistoryCompactionResult
    from dotclaw.runtime.application.ports import ContextPort, DelegationPort, LLMPort, ToolPort
    from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind, ToolCall
    import dotclaw.runtime.application.engine as engine_module
    import dotclaw.runtime.adapters.run_repository as run_repository_module
    import dotclaw.runtime.adapters.checkpoint_repository as checkpoint_repository_module

    has_recovery_protocol = __import__("importlib.util", fromlist=["find_spec"]).find_spec(
        "dotclaw.runtime.application.tool_recovery") is not None
    ToolRecoveryContract = getattr(dto_module, "ToolRecoveryContract", None)
    if has_recovery_protocol:
        from dotclaw.common.tool_replay import ToolExecutionEvidence, ToolReplayPolicy, ToolSideEffect

    source_root = Path(os.environ["HARNESS_SOURCE_ROOT"]).resolve()
    os.environ["HARNESS_ENGINE_MODULE"] = str(Path(engine_module.__file__).resolve())
    mode_phase = os.environ["HARNESS_PHASE"]
    stage = os.environ["HARNESS_STAGE"]
    policy = os.environ.get("HARNESS_POLICY", "no_auto_retry")
    validation_case = os.environ.get("HARNESS_VALIDATION_CASE", "")
    run_id = os.environ["HARNESS_RUN_ID"]
    session_id = os.environ["HARNESS_SESSION_ID"]
    call_id = os.environ["HARNESS_CALL_ID"]
    approval_id = os.environ.get("HARNESS_APPROVAL_ID", f"approval-{call_id}")
    provider = os.environ["HARNESS_PROVIDER"]
    barrier = sample / "engine-barrier.json"
    store = sample / "store"

    class Context(ContextPort):
        async def build(self, request, execution):
            return ContextBundle((), (), ContextMetadata(estimated_tokens=1))

        async def release_scope(self, owner, owner_key):
            return None

        async def release_all(self):
            return None

        def request_refresh(self, slot_id, owner, owner_key):
            return None

        def publish_signal(self, signal):
            return None

    class Policy:
        async def resolve(self, request):
            return AgentPolicySnapshot(request.agent_id, "pilot-v1", "pilot-model", 4,
                policy_data={"context_window": 64, "tokenizer_encoding": "cl100k_base"})

    class Counter:
        async def count(self, request):
            return TokenCountResult(1)

    class Compactor:
        async def compact_history(self, request):
            return HistoryCompactionResult("")

    class LLM(LLMPort):
        def __init__(self):
            self.calls = 0
            self.final_first = mode.startswith("recover")

        async def complete(self, context, execution, output_port=None):
            self.calls += 1
            if execution.policy.agent_id == "child-agent":
                return RunMessage("child-final", 2, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "子运行完成")
            if self.calls == 1 and not self.final_first:
                if stage == "delegation_backfill":
                    return RunMessage("response-delegate", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT,
                        "", tool_calls=(ToolCall(call_id, "delegate", {"target_agent_id": "child-agent",
                            "title": "isolated child", "objective": "complete child run"}),))
                call = ToolCall(call_id, "counted-effect", {"marker": call_id})
                return RunMessage("response-tools", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT,
                    "执行工具", tool_calls=(call,))
            return RunMessage("response-final", 3, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "完成")

    class Tool(ToolPort):
        async def preflight(self, invocation, execution):
            if has_recovery_protocol:
                return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED,
                    execution_evidence=ToolExecutionEvidence.NOT_STARTED)
            return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED)

        async def describe_recovery(self, call, execution):
            if has_recovery_protocol:
                if policy == "safe_retry":
                    return ToolRecoveryContract(policy=ToolReplayPolicy.SAFE_RETRY,
                        side_effect=ToolSideEffect.NONE, version="external-provider-v1",
                        recovery_max_retries=0 if validation_case == "safe_budget_zero" else 1,
                        recovery_initial_delay_s=0, execution_scope="pilot-safe-read")
                if policy == "keyed_idempotent":
                    contract_version = "external-provider-v1"
                    execution_scope = "pilot-keyed-effect"
                    if mode_phase == "recover" and validation_case == "contract_version_changed":
                        contract_version = "external-provider-v2"
                    if mode_phase == "recover" and validation_case == "execution_scope_changed":
                        execution_scope = "pilot-keyed-effect-v2"
                    return ToolRecoveryContract(policy=ToolReplayPolicy.KEYED_IDEMPOTENT,
                        side_effect=ToolSideEffect.PRESENT, version=contract_version,
                        key_valid_until=os.environ.get("HARNESS_KEY_VALID_UNTIL") if validation_case == "key_expired" else None,
                        recovery_max_retries=1, recovery_initial_delay_s=0, execution_scope=execution_scope)
                return ToolRecoveryContract(policy=ToolReplayPolicy.NO_AUTO_RETRY,
                    side_effect=ToolSideEffect.UNKNOWN, version="external-provider-v1")
            return None

        async def execute(self, invocation: ToolInvocation, execution: RunExecutionView):
            if stage in {"approval_approved", "approval_rejected"} and not invocation.approved:
                return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id=approval_id)
            payload = {"call_id": invocation.call.call_id, "run_id": execution.run_id, "policy": policy,
                "idempotency_key": getattr(invocation, "idempotency_key", None)}
            if mode_phase == "initial" and stage == "before_effect":
                _write_durable(barrier, {"stage": stage, **payload, "pid": os.getpid(),
                    "engine_module": str(Path(engine_module.__file__).resolve())})
                while True:
                    await asyncio.sleep(1)
            request = urllib.request.Request(provider, json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                effect = json.loads(response.read())
            if mode_phase == "initial" and stage in {"after_effect", "cancel_commit"}:
                _write_durable(barrier, {"stage": stage, **payload, "effect_id": effect["effect_id"],
                    "pid": os.getpid(), "engine_module": str(Path(engine_module.__file__).resolve())})
                while True:
                    await asyncio.sleep(1)
            if has_recovery_protocol:
                return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output=effect["effect_id"],
                    execution_evidence=ToolExecutionEvidence.RETURNED)
            return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output=effect["effect_id"])

    def build_child_engine():
        child_approvals = ApprovalRepositoryAdapter(store)
        child_runs = _PersistenceBarrier(RunRepositoryAdapter(store, approval_repository=child_approvals),
            sample, stage, mode_phase)
        child_checkpoints = CheckpointRepositoryAdapter(store)
        child_kwargs = {"token_counter": Counter(), "history_compactor": Compactor()}
        if has_recovery_protocol:
            child_kwargs["tool_recovery"] = ToolRecoveryService(child_runs, child_checkpoints, ApprovalService(child_approvals))
        return RuntimeEngine(child_runs, child_checkpoints, Context(), LLM(), Tool(), Policy(),
            ApprovalService(child_approvals), CancellationService(), **child_kwargs)

    class Delegation(DelegationPort):
        async def prepare(self, request: DelegationRequest):
            child_run_id = f"child-{uuid.uuid4().hex}"
            child_session_id = f"child-session-{uuid.uuid4().hex}"
            task_id = f"task-{child_run_id}"
            child_request = RunRequest(child_session_id, f"lease-{child_run_id}", request.target_agent_id,
                request.input_message, ConversationSnapshot(child_session_id, (), 0),
                parent_run_id=request.parent_run_id, root_run_id=request.root_run_id, run_id=child_run_id,
                delegation_task_id=task_id)
            await build_child_engine().prepare_run(child_request)
            _write_durable(sample / "child-association.json", {"child_run_id": child_run_id,
                "child_session_id": child_session_id, "parent_run_id": request.parent_run_id,
                "root_run_id": request.root_run_id, "task_id": task_id,
                "target_agent_id": request.target_agent_id, "input_message": request.input_message.content})
            return DelegationSubmission(child_run_id, task_id, child_session_id)

        async def start(self, child_run_id: str) -> None:
            await build_child_engine().start_prepared(child_run_id)

        async def resume(self, child_run_id: str) -> None:
            await build_child_engine().resume_run(child_run_id)

        async def result(self, child_run_id: str):
            child = json.loads((sample / "child-association.json").read_text(encoding="utf-8"))
            run = await runs.find_run(child_run_id)
            if run is None or not run.state.is_ended():
                return None
            messages = await runs.load_messages(child["child_session_id"], child_run_id)
            final = next((message for message in messages if message.message_id == run.final_message_id), None)
            return DelegationResult(child_run_id, run.state.outcome(), final.content if final else "")

        async def wait_terminal(self, child_run_id: str):
            result = await self.result(child_run_id)
            if result is None:
                raise RuntimeError("pilot 子运行尚未终态")
            return result

        async def cancel(self, child_run_id: str, reason: str, parent_request_id: str | None):
            await build_child_engine().request_cancel(child_run_id, reason)

    approval_repository = _PersistenceBarrier(ApprovalRepositoryAdapter(store), sample, stage, mode_phase)
    raw_runs = RunRepositoryAdapter(store, approval_repository=approval_repository)
    if stage == "cancel_commit" and mode_phase == "initial":
        persist_cancel_prepare = raw_runs._prepare_cancellation_commit_sync

        def persist_cancel_then_barrier(intent):
            persist_cancel_prepare(intent)
            _write_durable(sample / "cancel-barrier.json", {"stage": stage, "method": "_prepare_cancellation_commit_sync",
                "pid": os.getpid(), "run_id": run_id, "call_id": call_id,
                "engine_module": str(Path(engine_module.__file__).resolve())})
            while True:
                time.sleep(1)

        raw_runs._prepare_cancellation_commit_sync = persist_cancel_then_barrier
    runs = _PersistenceBarrier(raw_runs, sample, stage, mode_phase)
    checkpoints = _PersistenceBarrier(CheckpointRepositoryAdapter(store), sample, stage, mode_phase)
    kwargs = {"token_counter": Counter(), "history_compactor": Compactor()}
    try:
        from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
    except ImportError:
        tool_recovery = None
    else:
        tool_recovery = ToolRecoveryService(runs, checkpoints, ApprovalService(approval_repository))
    if tool_recovery is not None:
        kwargs["tool_recovery"] = tool_recovery
    delegation_port = Delegation() if stage == "delegation_backfill" else None
    engine = RuntimeEngine(runs, checkpoints, Context(), LLM(), Tool(), Policy(),
        ApprovalService(approval_repository), CancellationService(), delegation_port=delegation_port, **kwargs)
    operation_start = time.perf_counter()
    if mode in {"start", "perf_run"}:
        user = ConversationMessage("pilot-user", MessageRole.USER, "执行隔离效果样本", "2026-11-14T00:00:00+00:00")
        request = RunRequest(session_id, "pilot-lease", "pilot-agent", user,
            ConversationSnapshot(session_id, (), 0), run_id=run_id)
        result = await engine.execute(request)
    elif mode == "approve":
        result = await engine.resolve_approval(approval_id, os.environ["HARNESS_APPROVED"] == "true")
    elif mode == "cancel":
        await engine.request_cancel(run_id, "isolated fault pilot cancellation")
        result = await engine.complete_cancellation(run_id)
    elif mode == "recover_cancel":
        await engine.recover_cancellations()
        result = await engine.complete_cancellation(run_id)
    elif mode == "recover_delegation":
        child = json.loads((sample / "child-association.json").read_text(encoding="utf-8"))
        result = await engine.resume_delegation(child["child_run_id"])
    else:
        result = await engine.resume_run(run_id)
    engine_duration_ms = (time.perf_counter() - operation_start) * 1000
    run = await runs.find_run(run_id)
    checkpoint = await checkpoints.load(session_id, run_id)
    ledger_loader = getattr(runs, "load_tool_ledger", None)
    ledger = await ledger_loader(session_id, run_id) if ledger_loader else None
    events = await runs.load_events(session_id, run_id)
    messages = await runs.load_messages(session_id, run_id)
    source_modules = {"engine": str(Path(engine_module.__file__).resolve()),
        "run_repository": str(Path(run_repository_module.__file__).resolve()),
        "checkpoint_repository": str(Path(checkpoint_repository_module.__file__).resolve())}
    if has_recovery_protocol:
        import dotclaw.runtime.application.tool_recovery as recovery_module
        source_modules["tool_recovery"] = str(Path(recovery_module.__file__).resolve())
    result_path = sample / ("recover.json" if mode.startswith("recover") else f"{mode}.json")
    _write_durable(result_path, {"pid": os.getpid(), "engine_module": source_modules["engine"],
        "source_modules": source_modules, "engine_duration_ms": engine_duration_ms,
        "run_id": run_id, "run_outcome": result.state.outcome().value if result.state.outcome() else None,
        "control_status": getattr(result, "control_status", None), "error_code": result.error.code.value if result.error else None,
        "approval_id": result.approval_id,
        "run_state": run.state.to_dict() if run else None,
        "checkpoint_action": checkpoint.action.value if checkpoint else None,
        "ledger_states": [item.state.value for item in ledger.calls] if ledger else None,
        "ledger_has_intent": bool(ledger and ledger.pending_projection),
        "event_types": [event.event_type.value for event in events], "message_count": len(messages)})


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=("prepare", "recover"))
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--engine-pilot", action="store_true")
    parser.add_argument("--formal", action="store_true", help="运行固定故障窗套件；每版本每窗默认30个独立样本")
    parser.add_argument("--preflight-current", action="store_true", help="对新版固定正式case逐项执行一次pilot")
    parser.add_argument("--derive-formal-summary", type=Path, help="从正式原始JSONL生成按版本修正的派生汇总")
    parser.add_argument("--supplemental-rechecks", type=Path, help="基线投影快照补测目录，仅配合派生汇总使用")
    parser.add_argument("--supplemental-cancel-sample", type=Path, help="baseline取消窗口替补样本sample.json")
    parser.add_argument("--recheck-baseline-projections", action="store_true", help="补采baseline四个投影窗的恢复前后结果快照")
    parser.add_argument("--formal-repetitions", type=int, default=30)
    parser.add_argument("--perf", action="store_true")
    parser.add_argument("--perf-worker", action="store_true")
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--stage", choices=("before_effect", "after_effect", "ledger_result", "messages", "events",
        "checkpoint", "run", "intent_clear", "approval_approved", "approval_rejected", "cancel_commit",
        "delegation_backfill"), default="after_effect")
    parser.add_argument("--policy", choices=("no_auto_retry", "safe_retry", "keyed_idempotent"), default="no_auto_retry")
    parser.add_argument("--validation-case", choices=("safe_budget_zero", "key_expired", "contract_version_changed", "execution_scope_changed"))
    parser.add_argument("--child-engine", choices=("start", "approve", "cancel", "recover", "recover_cancel", "recover_delegation"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--current-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-root", type=Path, default=Path(r"D:\dev\dotClaw-recovery-baseline-61ad81f"))
    args = parser.parse_args()
    if args.perf_worker:
        _performance_worker(args.sample.resolve())
    elif args.child_engine:
        asyncio.run(_run_engine_child(args.child_engine, args.sample.resolve()))
    elif args.child:
        _child(args.child, args.sample.resolve())
    elif args.engine_pilot:
        print(json.dumps(run_engine_pilot((args.output or Path(".tmp/tool-recovery-faults")).resolve(),
            args.source_root.resolve(), args.stage, args.policy, validation_case=args.validation_case), ensure_ascii=False, indent=2))
    elif args.formal:
        roots = {"current": args.current_root.resolve(), "baseline": args.baseline_root.resolve()}
        formal_output = args.output.resolve() if args.output else Path("tmp") / f"tool-recovery-validation-{uuid.uuid4().hex[:12]}"
        print(json.dumps(run_formal(formal_output.resolve(), roots, args.formal_repetitions), ensure_ascii=False, indent=2))
    elif args.preflight_current:
        preflight_output = args.output.resolve() if args.output else Path("tmp") / f"tool-recovery-validation-{uuid.uuid4().hex[:12]}" / "preflight"
        print(json.dumps(run_current_preflight(preflight_output.resolve(), args.current_root.resolve()), ensure_ascii=False, indent=2))
    elif args.recheck_baseline_projections:
        recheck_output = args.output.resolve() if args.output else Path("tmp") / f"tool-recovery-validation-{uuid.uuid4().hex[:12]}" / "baseline-projection-rechecks"
        print(json.dumps(run_baseline_projection_rechecks(recheck_output.resolve(), args.baseline_root.resolve()), ensure_ascii=False, indent=2))
    elif args.derive_formal_summary:
        print(json.dumps(derive_formal_summary(args.derive_formal_summary.resolve(),
            args.supplemental_rechecks.resolve() if args.supplemental_rechecks else None,
            args.supplemental_cancel_sample.resolve() if args.supplemental_cancel_sample else None), ensure_ascii=False, indent=2))
    elif args.perf:
        print(json.dumps(run_performance((args.output or Path(".tmp/tool-recovery-faults")).resolve(), args.current_root.resolve(), args.baseline_root.resolve(),
            args.repetitions, args.warmups), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_pilot((args.output or Path(".tmp/tool-recovery-faults")).resolve(), args.source_root.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
