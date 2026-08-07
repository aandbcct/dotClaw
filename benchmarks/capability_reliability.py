"""PR5 Capability 安全链实验 CLI（命令行入口）与工件写出。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotclaw.tools.base import ToolExecutionContext
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.policy import PolicyDecision, PolicyScope, default_policy_scope
from dotclaw.tools.schema import validate_args

from .capability_matrix import SENSITIVE_MARKER, SUITE_CAPABILITY, CapabilityMatrixCase, build_registry, load_matrix
from .capability_observers import ChainObservation, CountingApprovalManager, CountingBroker, CountingHandler, CountingPolicyEngine, RecordingJournal
from .capability_stats import performance_summary, summarize_security
from .eval_baseline_models import BenchmarkSample
from .eval_baseline_stats import build_snapshot


class _Channel:
    """固定审批 Channel（通道）替身，仅返回矩阵预设结果。"""
    def __init__(self, response: str) -> None:
        self._response = response
    async def ask_user(self, prompt: str) -> str:
        return self._response


def _commit() -> str:
    """读取当前短提交号；非 Git 环境显式标记 unknown。"""
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _matrix_hash(matrix: Path) -> str:
    """计算矩阵原始字节的稳定摘要，避免配置变更被固定占位掩盖。"""
    return hashlib.sha256(matrix.read_bytes()).hexdigest()[:16]


def _scope(case: CapabilityMatrixCase, workspace: Path) -> PolicyScope:
    """从矩阵行构造隔离策略作用域。"""
    scope = default_policy_scope(str(workspace))
    for profile, decision in (case.global_rules or {}).items():
        scope.global_rules[profile] = PolicyDecision(decision)
    scope.network_services = dict(case.network_services or {})
    if case.allowed_mcp_servers is not None:
        scope.allowed_mcp_servers = list(case.allowed_mcp_servers)
    return scope


def _executor(case: CapabilityMatrixCase, workspace: Path, observation: ChainObservation) -> ToolExecutor:
    """装配每 Case 独立的生产链替身，避免规则与计数跨行泄漏。"""
    registry = build_registry()
    for name in registry.all_names():
        registry.unregister(name)
    # 每个 Handler 只外包计数，不改变其 ToolDefinition 或执行语义。
    original = build_registry()
    for name in original.all_names():
        registry.register(CountingHandler(original.get(name), observation))
    rules = case.agent_rules or {}
    return ToolExecutor(registry, approval_manager=CountingApprovalManager(observation), policy_engine=CountingPolicyEngine(observation, _scope(case, workspace)), capability_broker=CountingBroker(observation), agent_policy_resolver=lambda agent_id: dict(rules.get(agent_id, {})))


async def run_matrix_case(case: CapabilityMatrixCase, attempt: int = 0, is_warmup: bool = False, matrix_hash: str = "") -> BenchmarkSample:
    """执行一行安全矩阵；Windows 联接点能力不足时只记录环境跳过。"""
    if case.windows_junction:
        return await _run_junction_case(case, attempt, is_warmup, matrix_hash)
    with tempfile.TemporaryDirectory(prefix="dotclaw-capability-") as temporary:
        workspace = Path(temporary)
        observation = ChainObservation()
        executor = _executor(case, workspace, observation)
        channel = None if case.approval == "none" else _Channel("y" if case.approval == "approve" else "n")
        started = time.perf_counter()
        if case.pre_approved:
            result = await executor.execute_approved(case.tool, dict(case.arguments), ToolExecutionContext(agent_id=case.agent_id), RecordingJournal(observation))
        else:
            result = await executor.execute(case.tool, dict(case.arguments), channel, RecordingJournal(observation), ToolExecutionContext(agent_id=case.agent_id))
        return _sample(case, attempt, is_warmup, observation, result, elapsed=(time.perf_counter() - started) * 1000.0, config_hash=matrix_hash)


async def _run_junction_case(case: CapabilityMatrixCase, attempt: int, is_warmup: bool, matrix_hash: str) -> BenchmarkSample:
    """在 Windows 可建立联接点时执行真实解析；不可建立则保留明确环境跳过。"""
    if os.name != "nt":
        return _sample(case, attempt, is_warmup, ChainObservation(), None, skipped="当前平台不是 Windows", config_hash=matrix_hash)
    with tempfile.TemporaryDirectory(prefix="dotclaw-capability-junction-") as temporary:
        root = Path(temporary)
        workspace, outside, link = root / "workspace", root / "outside", root / "workspace" / "junction-outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "sentinel.txt").write_text("outside", encoding="utf-8")
        try:
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True, check=False)
            if created.returncode != 0:
                return _sample(case, attempt, is_warmup, ChainObservation(), None, skipped=f"当前用户无法创建 Windows Junction：{created.stderr.strip() or created.stdout.strip()}", config_hash=matrix_hash)
            observation = ChainObservation()
            executor = _executor(case, workspace, observation)
            result = await executor.execute(case.tool, dict(case.arguments), None, RecordingJournal(observation), ToolExecutionContext())
            return _sample(case, attempt, is_warmup, observation, result, config_hash=matrix_hash)
        finally:
            if link.exists():
                os.rmdir(link)


def _sample(case: CapabilityMatrixCase, attempt: int, is_warmup: bool, observation: ChainObservation, result: Any, *, elapsed: float = 0.0, skipped: str | None = None, measurement_mode: str | None = None, duration: float | None = None, config_hash: str = "") -> BenchmarkSample:
    """将一次观察规范为统一 BenchmarkSample（单次采样记录）。"""
    error = None if result is None else result.error_code
    actual = "ALLOW" if result is not None and not result.is_error else error
    expected_ok = skipped is None and actual == case.expected
    requests = observation.requests
    request = requests[0] if requests else None
    path_match = None
    if request is not None and request.absolute_path is not None and observation.handler_paths:
        path_match = observation.handler_paths[0] == request.absolute_path
    leaks = observation.sensitive_leak_count(SENSITIVE_MARKER)
    return BenchmarkSample(dataset=SUITE_CAPABILITY, case_id=case.case_id, attempt=attempt, is_warmup=is_warmup, git_commit=_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash=config_hash, eval_schema_version="tool-v1", passed=expected_ok, failure_kind=None if expected_ok else ("environment_skip" if skipped else "security_assertion"), assertions_passed=int(expected_ok), assertions_total=1, trace_available=True, wall_duration_ms=elapsed, run_id=None, suite=SUITE_CAPABILITY, scenario_id=case.case_id, matrix_case_id=case.case_id, expected_decision=case.expected, actual_decision=actual, actual_error_code=error, decision_pass=None if skipped else expected_ok, validation_entered=0 if error == "INVALID_ARGUMENTS" else 1, broker_entered=observation.broker_entered, policy_entered=observation.policy_entered, approval_entered=observation.approval_entered, handler_entered=observation.handler_entered, resource_kind=None if request is None else request.kind.value, policy_profile=None if request is None else request.profile, matched_rule=None if observation.outcome is None else observation.outcome.matched_rule, resolved_path_match=path_match, network_service=None if request is None else request.service, network_host=None if request is None else request.host, mcp_server=None if request is None else request.server, journal_summary_redacted=leaks == 0, approval_summary_redacted=leaks == 0, sensitive_leak_count=leaks, agent_id=case.agent_id or None, agent_rule_source=case.agent_id or None, agent_policy_isolated=True if case.agent_id else None, measurement_mode=measurement_mode, pre_handler_duration_ms=duration, capability_reason=skipped)


async def run_performance(repeat: int, warmup: int, matrix_hash: str) -> list[BenchmarkSample]:
    """以同一个 allow Handler 与调用上下文采集直接/完整链 Handler-entry 时延。"""
    case = CapabilityMatrixCase("performance_allow", "cap.file.read", {"path": "notes.txt"}, "ALLOW")
    samples: list[BenchmarkSample] = []
    for mode in ("direct_handler", "full_security_chain"):
        for attempt in range(warmup + repeat):
            with tempfile.TemporaryDirectory(prefix="dotclaw-capability-perf-") as temporary:
                observation = ChainObservation()
                executor = _executor(case, Path(temporary), observation)
                handler = executor.get_handler(case.tool)
                arguments = validate_args(handler.args_model, dict(case.arguments))
                if mode == "direct_handler":
                    start = time.perf_counter()
                    await handler.execute(arguments, ToolExecutionContext())
                else:
                    start = time.perf_counter()
                    await executor.execute(case.tool, arguments, None, RecordingJournal(observation), ToolExecutionContext())
                if observation.handler_entry_at is None:
                    raise ValueError("性能采样未进入 Handler")
                samples.append(_sample(case, attempt, attempt < warmup, observation, type("Result", (), {"is_error": False, "error_code": None})(), measurement_mode=mode, duration=(observation.handler_entry_at - start) * 1000.0, config_hash=matrix_hash))
    return samples


async def run_suite(matrix: Path, performance_warmup: int, performance_repeat: int) -> list[BenchmarkSample]:
    """执行一次完整有限矩阵与可选性能采样。"""
    matrix_hash = _matrix_hash(matrix)
    samples = [await run_matrix_case(case, matrix_hash=matrix_hash) for case in load_matrix(matrix)]
    samples.extend(await run_performance(performance_repeat, performance_warmup, matrix_hash))
    return samples


def write_artifacts(samples: list[BenchmarkSample], output: Path, baseline: Path | None, matrix: Path) -> None:
    """写出 JSONL、配置与两份 Markdown 报告；可选复制为基线。"""
    output.mkdir(parents=True, exist_ok=True)
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + _commit()
    sample_path = output / "samples" / f"{identifier}.jsonl"
    sample_path.parent.mkdir(exist_ok=True)
    sample_path.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in samples), encoding="utf-8")
    snapshot = build_snapshot(
        snapshot_id=identifier, generated_at=datetime.now(UTC).isoformat(), git_commit=_commit(),
        dataset=SUITE_CAPABILITY,
        environment={"python_version": sys.version.split()[0], "platform": platform.platform(), "config_hash": samples[0].config_hash, "eval_schema_version": "tool-v1"},
        warmup=sum(1 for item in samples if item.is_warmup), repeat=sum(1 for item in samples if not item.is_warmup),
        samples=samples, samples_path=f"samples/{identifier}.jsonl", scenario_id=SUITE_CAPABILITY,
        samples_content_summary={"line_count": len(samples), "byte_count": sample_path.stat().st_size},
    )
    snapshot_path = output / f"{identifier}.json"
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize_security(samples)
    direct, full = performance_summary(samples, "direct_handler"), performance_summary(samples, "full_security_chain")
    (output / "security-matrix.md").write_text(f"# PR5 安全矩阵\n\n适用：{summary.applicable}；通过：{summary.passed}；失败：{summary.failed}；环境跳过：{summary.skipped}。\n\n- 参数校验失败 Handler 进入：{summary.invalid_handler_entries}\n- Policy deny Handler 进入：{summary.denied_handler_entries}\n- 未获审批 Handler 进入：{summary.unapproved_handler_entries}\n- 敏感标记泄露：{summary.sensitive_leak_count}\n\n路径一致性仅证明 Broker 资源与 Handler 入参一致，不构成 TOCTOU 防护。进程结论仅限档案级策略。\n", encoding="utf-8")
    (output / "security-chain-overhead.md").write_text(f"# PR5 安全链前置开销\n\n| 模式 | N | P50 ms | P95 ms | 最大 ms |\n|---|---:|---:|---:|---:|\n| 直接 Handler | {direct['sample_count']} | {direct['p50_ms']:.4f} | {direct['p95_ms']:.4f} | {direct['max_ms']:.4f} |\n| 完整安全链 | {full['sample_count']} | {full['p50_ms']:.4f} | {full['p95_ms']:.4f} | {full['max_ms']:.4f} |\n\n完整链减直接 Handler：P50 {full['p50_ms']-direct['p50_ms']:.4f} ms，P95 {full['p95_ms']-direct['p95_ms']:.4f} ms。\n", encoding="utf-8")
    shutil.copy2(matrix, output / "matrix-config.json")
    if baseline is not None:
        baseline.mkdir(parents=True, exist_ok=True)
        baseline_samples = baseline / "samples"
        baseline_samples.mkdir(exist_ok=True)
        shutil.copy2(sample_path, baseline_samples / f"{identifier}.jsonl")
        shutil.copy2(snapshot_path, baseline / f"{identifier}.json")


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 参数；正式采样次数必须由调用者显式给出。"""
    parser = argparse.ArgumentParser(description="PR5 Capability 安全链实验")
    parser.add_argument("--suite", default=SUITE_CAPABILITY)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--performance-warmup", type=int, default=0)
    parser.add_argument("--performance-repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--save-baseline", type=Path)
    args = parser.parse_args(argv)
    if args.suite != SUITE_CAPABILITY or args.performance_warmup < 0 or args.performance_repeat <= 0:
        parser.error("suite 或性能采样参数不合法")
    write_artifacts(asyncio.run(run_suite(args.matrix, args.performance_warmup, args.performance_repeat)), args.output, args.save_baseline, args.matrix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
