"""PR4 恢复可靠性套件 CLI（命令行入口）与 JSONL（逐行 JSON）工件写出。"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from dotclaw.runtime.adapters import RunRepositoryAdapter
from dotclaw.runtime.domain.context import SuccessCommitFaultPoint

from .eval_baseline_models import BenchmarkSample, CapabilityStatus, ExternalEffectStatus, RecoveryFaultScenario, SUITE_RECOVERY
from .eval_baseline_stats import build_snapshot
from .recovery_assertions import checkpoint_summary, collect_recovery_facts, expected_checkpoint_action
from .recovery_faults import EffectLog, audit_delegation_cold_rebuild, interrupt_initial_run, make_engine, prepare_success_commit_fault, recover_success_commit, start_waiting_approval
from .recovery_subprocess import run_subprocess_recovery
from .recovery_stats import aggregate_recovery_scenarios


_FORMAL_MODES: tuple[str, ...] = (
    "llm_before_send_failure", "llm_response_unknown", "tool_before_effect", "tool_after_effect",
    "approval_cold_rebuild",
)


def _git_commit() -> str:
    """读取当前提交；无 Git（版本库）环境时保存明确占位。"""
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


async def run_recovery_sample(root: Path, mode: str, attempt: int, is_warmup: bool) -> BenchmarkSample:
    """执行一个受控异常场景，丢弃旧服务后从同一根目录冷重建并恢复。"""
    session_id = f"recovery-{mode}-{attempt}"
    started = time.perf_counter()
    if mode == "approval_cold_rebuild":
        fault = await start_waiting_approval(root, session_id)
        session_id = fault.session_id
        before_action, _ = await checkpoint_summary(root, session_id, fault.run_id)
        before_context_count = len(await RunRepositoryAdapter(root).load_context_versions(session_id, fault.run_id))
        restart_started = time.perf_counter()
        result = await make_engine(root, mode, resume=True).resolve_approval("recovery-approval", True)
    else:
        fault = await interrupt_initial_run(root, mode, session_id)
        session_id = fault.session_id
        before_action, _ = await checkpoint_summary(root, session_id, fault.run_id)
        before_context_count = len(await RunRepositoryAdapter(root).load_context_versions(session_id, fault.run_id))
        restart_started = time.perf_counter()
        result = await make_engine(root, mode, resume=True).resume_run(fault.run_id)
    finished = time.perf_counter()
    facts = await collect_recovery_facts(root, session_id, fault.run_id, before_action=before_action, before_context_count=before_context_count)
    expected_action = expected_checkpoint_action(mode).value
    expected_before_action = "suspend" if mode == "approval_cold_rebuild" else expected_action
    control_pass = facts.control_recovery_pass and before_action == expected_before_action and result.run_id == fault.run_id
    effects = EffectLog(root)
    llm_count, tool_count = effects.count("llm_request"), effects.count("tool_effect")
    status = _external_status(mode, llm_count, tool_count)
    duplicate_count = max(0, tool_count - 1) if mode.startswith("tool_") else max(0, llm_count - 1)
    expected_tool_results = 2 if mode == "approval_cold_rebuild" else (1 if mode.startswith("tool_") else 0)
    internal_pass = facts.internal_facts_pass and facts.tool_result_count == expected_tool_results
    scenario = RecoveryFaultScenario(mode)
    return BenchmarkSample(
        dataset=SUITE_RECOVERY, case_id=mode, attempt=attempt, is_warmup=is_warmup,
        git_commit=_git_commit(), python_version=sys.version.split()[0], platform=platform.platform(),
        config_hash="recovery-v1-fixed", eval_schema_version="runtime-v4", passed=control_pass and internal_pass,
        failure_kind=None if control_pass and internal_pass else "recovery_assertion",
        assertions_passed=sum((control_pass, internal_pass)), assertions_total=2, trace_available=True,
        wall_duration_ms=(finished - started) * 1000.0, run_id=fault.run_id,
        suite=SUITE_RECOVERY, scenario_id=mode, evidence_summary={"effect_log": effects.read(), "session_id": session_id},
        fault_scenario=scenario, fault_point=before_action, fault_mechanism="controlled_base_exception" if mode != "approval_cold_rebuild" else "cold_rebuild_approval",
        restart_kind="cold_rebuild", rebuild_count=1, checkpoint_action_before=before_action,
        checkpoint_action_resumed=expected_action, same_run_id=facts.same_run_id, same_context_version=facts.same_context_version,
        control_recovery_pass=control_pass, tool_result_count=facts.tool_result_count,
        state_transition_count=facts.state_transition_count, completed_event_count=facts.completed_event_count,
        conversation_projection_count=facts.conversation_projection_count, checkpoint_cleaned=facts.checkpoint_cleaned,
        success_intent_cleaned=facts.success_intent_cleaned, internal_facts_pass=internal_pass,
        llm_request_sent_count=llm_count, tool_effect_count=tool_count, external_duplicate_count=duplicate_count,
        external_effect_status=status, fault_to_restart_ms=(restart_started - started) * 1000.0,
        restart_to_terminal_ms=(finished - restart_started) * 1000.0, recovery_wall_duration_ms=(finished - restart_started) * 1000.0,
        capability_status=CapabilityStatus.FORMAL,
    )


async def run_success_commit_sample(root: Path, point: SuccessCommitFaultPoint, attempt: int, is_warmup: bool) -> BenchmarkSample:
    """执行一个成功提交边界并重复恢复，记录唯一投影、事件与终态。"""
    started = time.perf_counter()
    session_id, run_id, injector = await prepare_success_commit_fault(root, point)
    restarted = time.perf_counter()
    await recover_success_commit(root, injector)
    finished = time.perf_counter()
    facts = await collect_recovery_facts(root, session_id, run_id, before_action=None, before_context_count=0)
    internal_pass = facts.internal_facts_pass and facts.tool_result_count == 0
    return BenchmarkSample(
        dataset=SUITE_RECOVERY, case_id=f"success_commit:{point.value}", attempt=attempt, is_warmup=is_warmup,
        git_commit=_git_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash="recovery-v1-fixed", eval_schema_version="runtime-v4",
        passed=internal_pass, failure_kind=None if internal_pass else "success_commit_assertion", assertions_passed=int(internal_pass), assertions_total=1, trace_available=True,
        wall_duration_ms=(finished-started)*1000.0, run_id=run_id, suite=SUITE_RECOVERY, scenario_id=RecoveryFaultScenario.SUCCESS_COMMIT.value,
        fault_scenario=RecoveryFaultScenario.SUCCESS_COMMIT, fault_point=point.value, fault_mechanism="success_commit_fault_port", restart_kind="cold_rebuild", rebuild_count=1,
        same_run_id=facts.same_run_id, same_context_version=True, control_recovery_pass=internal_pass, tool_result_count=0, state_transition_count=facts.state_transition_count,
        completed_event_count=facts.completed_event_count, conversation_projection_count=facts.conversation_projection_count, checkpoint_cleaned=facts.checkpoint_cleaned,
        success_intent_cleaned=facts.success_intent_cleaned, internal_facts_pass=internal_pass, external_effect_status=ExternalEffectStatus.NOT_APPLICABLE,
        fault_to_restart_ms=(restarted-started)*1000.0, restart_to_terminal_ms=(finished-restarted)*1000.0, recovery_wall_duration_ms=(finished-restarted)*1000.0,
        capability_status=CapabilityStatus.FORMAL,
    )


async def run_process_sample(root: Path, attempt: int, is_warmup: bool) -> BenchmarkSample:
    """执行唯一的工具前子进程强退代表性验证。"""
    result = await run_subprocess_recovery(root)
    passed = result.control_pass and result.internal_pass and result.tool_effect_count == 1
    return BenchmarkSample(
        dataset=SUITE_RECOVERY, case_id="tool_before_effect_subprocess", attempt=attempt, is_warmup=is_warmup,
        git_commit=_git_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash="recovery-v1-fixed", eval_schema_version="runtime-v4",
        passed=passed, failure_kind=None if passed else "subprocess_recovery_assertion", assertions_passed=int(passed), assertions_total=1, trace_available=True,
        wall_duration_ms=result.recovery_ms, run_id=result.run_id, suite=SUITE_RECOVERY, scenario_id=RecoveryFaultScenario.TOOL_BEFORE_EFFECT.value,
        fault_scenario=RecoveryFaultScenario.TOOL_BEFORE_EFFECT, fault_point="execute_tools_subprocess", fault_mechanism="subprocess_forced_exit", restart_kind="new_process", rebuild_count=1,
        checkpoint_action_before="execute_tools", checkpoint_action_resumed="execute_tools", same_run_id=result.control_pass, same_context_version=result.control_pass,
        control_recovery_pass=result.control_pass, tool_result_count=1, internal_facts_pass=result.internal_pass, tool_effect_count=result.tool_effect_count,
        external_duplicate_count=0, external_effect_status=ExternalEffectStatus.ONCE, recovery_wall_duration_ms=result.recovery_ms, capability_status=CapabilityStatus.FORMAL,
        evidence_summary=result.evidence_summary,
    )


async def delegation_boundary_sample(root: Path, attempt: int, is_warmup: bool) -> BenchmarkSample:
    """执行真实父 Run（父级运行）挂起、冷重建与回灌失败审计。"""
    evidence = await audit_delegation_cold_rebuild(root)
    reason = str(evidence["failure"])
    return BenchmarkSample(
        dataset=SUITE_RECOVERY, case_id="delegation_cold_rebuild_boundary", attempt=attempt, is_warmup=is_warmup,
        git_commit=_git_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash="recovery-v1-fixed", eval_schema_version="runtime-v4",
        passed=False, failure_kind="capability_boundary", assertions_passed=0, assertions_total=0, trace_available=False, wall_duration_ms=0.0, run_id=None,
        suite=SUITE_RECOVERY, scenario_id=RecoveryFaultScenario.DELEGATION_COLD_REBUILD_BOUNDARY.value,
        fault_scenario=RecoveryFaultScenario.DELEGATION_COLD_REBUILD_BOUNDARY, fault_point="parent_wait_mapping", fault_mechanism="capability_audit", restart_kind="cold_rebuild",
        capability_status=CapabilityStatus.BOUNDARY, capability_reason=reason, external_effect_status=ExternalEffectStatus.NOT_APPLICABLE, evidence_summary=evidence,
    )


def _external_status(mode: str, llm_count: int, tool_count: int) -> ExternalEffectStatus:
    """按计划将未知 LLM 结果与可观察工具重复分层表达。"""
    if mode == "llm_response_unknown":
        return ExternalEffectStatus.UNKNOWN
    if mode.startswith("llm_"):
        return ExternalEffectStatus.ONCE if llm_count == 1 else ExternalEffectStatus.DUPLICATE_OBSERVED
    if tool_count == 0:
        return ExternalEffectStatus.NOT_OCCURRED
    return ExternalEffectStatus.ONCE if tool_count == 1 else ExternalEffectStatus.DUPLICATE_OBSERVED


async def run_suite(*, warmup: int, repeat: int, process_warmup: int = 0, process_repeat: int = 0) -> list[BenchmarkSample]:
    """在每轮独立存储根执行所有普通受控异常场景。"""
    samples: list[BenchmarkSample] = []
    for mode in _FORMAL_MODES:
        for attempt in range(warmup + repeat):
            with tempfile.TemporaryDirectory(prefix="dotclaw-recovery-") as temporary:
                samples.append(await run_recovery_sample(Path(temporary), mode, attempt, attempt < warmup))
    for point in SuccessCommitFaultPoint:
        for attempt in range(warmup + repeat):
            with tempfile.TemporaryDirectory(prefix="dotclaw-success-commit-") as temporary:
                samples.append(await run_success_commit_sample(Path(temporary), point, attempt, attempt < warmup))
    for attempt in range(process_warmup + process_repeat):
        with tempfile.TemporaryDirectory(prefix="dotclaw-subprocess-") as temporary:
            samples.append(await run_process_sample(Path(temporary), attempt, attempt < process_warmup))
    for attempt in range(warmup + repeat):
        with tempfile.TemporaryDirectory(prefix="dotclaw-delegation-boundary-") as temporary:
            samples.append(await delegation_boundary_sample(Path(temporary), attempt, attempt < warmup))
    return samples


def write_artifacts(samples: list[BenchmarkSample], output_dir: Path, baseline_dir: Path | None = None) -> None:
    """写出可追溯 JSONL、分层报告、故障配置和能力边界声明。"""
    _validate_comparable_samples(samples)
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    snapshot_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + _git_commit()
    samples_path = output_dir / "samples" / f"{snapshot_id}.jsonl"
    samples_path.parent.mkdir(exist_ok=True)
    samples_path.write_text("".join(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n" for sample in samples), encoding="utf-8")
    content_summary = {"line_count": len(samples), "byte_count": samples_path.stat().st_size}
    snapshot_samples = [sample for sample in samples if sample.capability_status is CapabilityStatus.FORMAL]
    snapshot = build_snapshot(
        snapshot_id=snapshot_id, generated_at=datetime.now(UTC).isoformat(), git_commit=_git_commit(),
        dataset=SUITE_RECOVERY, environment={"python_version": sys.version.split()[0], "platform": platform.platform(), "config_hash": "recovery-v1-fixed", "eval_schema_version": "runtime-v4"},
        warmup=sum(1 for sample in snapshot_samples if sample.is_warmup), repeat=sum(1 for sample in snapshot_samples if not sample.is_warmup),
        samples=snapshot_samples, samples_path=f"samples/{snapshot_id}.jsonl", scenario_id=SUITE_RECOVERY, samples_content_summary=content_summary,
    )
    snapshot_path = output_dir / f"{snapshot_id}.json"
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    summaries = aggregate_recovery_scenarios(samples)
    lines = ["# PR4 操作节点故障注入与恢复", "", "仅统计正式非预热样本；外部副作用不并入内部事实成功率。", "", "| 场景 | 控制状态 | 内部事实 | 恢复 P50/P95 (ms) | 外部副作用 |", "|---|---:|---:|---:|---|"]
    for item in summaries:
        label = item.scenario.value if item.fault_point is None else f"{item.scenario.value}:{item.fault_point}"
        lines.append(f"| {label} | {item.control_passed_count}/{item.sample_count} | {item.internal_passed_count}/{item.sample_count} | {item.recovery_p50_ms}/{item.recovery_p95_ms} | {dict(item.external_effect_status_counts)} |")
    lines.extend(["", "## 边界", "", "LLM 响应未知与工具执行后中断不承诺跨崩溃 exactly-once；委派等待冷重建不计入本报告成功率。", "", "## 原始证据", "", f"- `samples/{snapshot_id}.jsonl`", f"- `{snapshot_id}.json`", "- `fault-config.json`", ""])
    (output_dir / "recovery-report.md").write_text("\n".join(lines), encoding="utf-8")
    boundaries = [sample for sample in samples if sample.capability_status is CapabilityStatus.BOUNDARY]
    boundary_lines = ["# 能力边界", "", "以下样本保存在原始 JSONL，但不会进入快照 global_summary（全局汇总）或正式恢复成功率。", ""]
    for sample in boundaries:
        boundary_lines.append(f"- `{sample.case_id}`：{sample.capability_reason}")
    (output_dir / "capability-boundary.md").write_text("\n".join(boundary_lines) + "\n", encoding="utf-8")
    (output_dir / "fault-config.json").write_text(json.dumps({"suite": SUITE_RECOVERY, "scenarios": list(_FORMAL_MODES), "git_commit": _git_commit()}, ensure_ascii=False, indent=2), encoding="utf-8")
    (evidence_dir / "samples-summary.json").write_text(json.dumps([sample.evidence_summary for sample in samples], ensure_ascii=False, indent=2), encoding="utf-8")
    if baseline_dir is not None:
        baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline_samples = baseline_dir / "samples" / f"{snapshot_id}.jsonl"
        baseline_samples.parent.mkdir(exist_ok=True)
        shutil.copy2(samples_path, baseline_samples)
        shutil.copy2(snapshot_path, baseline_dir / f"{snapshot_id}.json")


def _validate_comparable_samples(samples: list[BenchmarkSample]) -> None:
    """拒绝不同提交或固定故障配置混入同一恢复成本快照。"""
    formal = [sample for sample in samples if not sample.is_warmup and sample.capability_status is CapabilityStatus.FORMAL]
    if not formal:
        raise ValueError("恢复工件至少需要一个正式样本")
    commits = {sample.git_commit for sample in formal}
    configs = {sample.config_hash for sample in formal}
    schemas = {sample.eval_schema_version for sample in formal}
    if len(commits) != 1 or len(configs) != 1 or len(schemas) != 1:
        raise ValueError("正式恢复样本的源码提交、故障配置或 Runtime schema 不一致")


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 参数；默认值仅供开发期小样本，正式采样由调用者显式指定。"""
    parser = argparse.ArgumentParser(description="PR4 操作节点恢复可靠性套件")
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--process-warmup", type=int, default=0)
    parser.add_argument("--process-repeat", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--save-baseline", type=Path)
    args = parser.parse_args(argv)
    if args.warmup < 0 or args.repeat <= 0 or args.process_warmup < 0 or args.process_repeat < 0:
        parser.error("warmup 必须 >= 0、repeat 必须 > 0，process 参数必须 >= 0")
    samples = asyncio.run(run_suite(warmup=args.warmup, repeat=args.repeat, process_warmup=args.process_warmup, process_repeat=args.process_repeat))
    write_artifacts(samples, args.output, args.save_baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
