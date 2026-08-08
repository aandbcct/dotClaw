"""PR7 委派可靠性 CLI：只编排固定 Fixture，输出 JSONL、快照和局部报告。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from .delegation_assertions import assert_cancellation, assert_delegation_chain, passed
from .delegation_stats import summarize
from .delegation_workloads import ChildOutcome, DelegationWorkloadConfig, chain_request_id, run_child_outcome, run_concurrent_completed, run_parent_cancellation
from .eval_baseline_models import BenchmarkSample, SUITE_DELEGATION
from .eval_baseline_stats import build_snapshot, percentile


def _commit() -> str:
    """读取固定短提交；无法读取时不伪造正式证据。"""
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


async def _outcome_sample(root: Path, outcome: ChildOutcome, attempt: int, warmup: bool, config: DelegationWorkloadConfig, parent_index: int = 0, facts_override: Mapping[str, object] | None = None) -> BenchmarkSample:
    """规范化一个已由固定 Fixture 观测的链路记录。

    读取真实 RuntimeDelegationAdapter（运行时委派适配器）持久化事实，不引入
    Benchmark 专用生产状态。
    """
    request_id = chain_request_id(parent_index, attempt)
    facts = facts_override if facts_override is not None else await run_child_outcome(root, config, request_id, outcome)
    sample = BenchmarkSample(dataset=SUITE_DELEGATION, suite=SUITE_DELEGATION, case_id=outcome.value,
        attempt=attempt, is_warmup=warmup, git_commit=_commit(), python_version=sys.version.split()[0],
        platform=platform.platform(), config_hash=hashlib.sha256(json.dumps(config.to_dict(), sort_keys=True).encode()).hexdigest()[:16],
        eval_schema_version="runtime-v4", passed=False, failure_kind=None, assertions_passed=0,
        assertions_total=10, trace_available=True, wall_duration_ms=float(facts["parent_end_to_end_ms"]), run_id=str(facts["parent_run_id"]),
        parent_run_id=str(facts["parent_run_id"]), child_run_id=str(facts["child_run_id"]), task_id=str(facts["task_id"]), parent_session_id=str(facts["parent_session_id"]), child_session_id=str(facts["child_session_id"]),
        target_agent_id=str(facts["target_agent_id"]), chain_request_id=request_id, child_outcome=str(facts["child_outcome"]), parent_outcome=str(facts["parent_outcome"]),
        delegation_submit_count=int(facts["delegation_submit_count"]), result_backfill_count=int(facts["result_backfill_count"]), delegation_submitted_event_count=int(facts["delegation_submitted_event_count"]), delegation_completed_event_count=int(facts["delegation_completed_event_count"]),
        cross_chain_message_count=facts.get("cross_chain_message_count"), cross_chain_context_count=facts.get("cross_chain_context_count"), cross_chain_tool_count=facts.get("cross_chain_tool_count"), cross_chain_stream_count=facts.get("cross_chain_stream_count"), misdelivery_count=facts.get("misdelivery_count"),
        suspend_to_backfill_ms=float(facts["suspend_to_backfill_ms"]), parent_end_to_end_ms=float(facts["parent_end_to_end_ms"]),
        fixture_version=config.fixture_version, environment={"python_version": sys.version.split()[0], "platform": platform.platform()},
        fixture_fingerprint=config.fixture_version, formal_sampling=not warmup, evidence_summary={"request_id": request_id})
    checks = assert_delegation_chain(sample)
    return BenchmarkSample(**{**sample.__dict__, "passed": passed(checks), "assertions_passed": sum(check.passed for check in checks)})


async def _cancellation_sample(root: Path, attempt: int, warmup: bool, config: DelegationWorkloadConfig) -> BenchmarkSample:
    """将父取消真实观测转换为统一采样记录。"""
    facts = await run_parent_cancellation(root, config, f"cancellation-{attempt}")
    sample = BenchmarkSample(dataset=SUITE_DELEGATION, suite=SUITE_DELEGATION, case_id="parent_cancellation", attempt=attempt, is_warmup=warmup, git_commit=_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash=hashlib.sha256(json.dumps(config.to_dict(), sort_keys=True).encode()).hexdigest()[:16], eval_schema_version="runtime-v4", passed=False, failure_kind=None, assertions_passed=0, assertions_total=5, trace_available=True, wall_duration_ms=float(facts["parent_cancel_effect_ms"]), run_id=str(facts["parent_run_id"]), parent_run_id=str(facts["parent_run_id"]), child_run_id=str(facts["child_run_id"]), parent_session_id=f"parent-cancellation-{attempt}", child_outcome="cancelled", parent_outcome="cancelled", cancel_delivery_ms=float(facts["cancel_delivery_ms"]), parent_cancel_effect_ms=float(facts["parent_cancel_effect_ms"]), child_cancel_effect_ms=float(facts["child_cancel_effect_ms"]), followup_started=bool(facts["followup_started"]), followup_completed=bool(facts["followup_completed"]), fixture_version=config.fixture_version, fixture_fingerprint=config.fixture_version, environment={"python_version": sys.version.split()[0], "platform": platform.platform()}, formal_sampling=not warmup, evidence_summary=dict(facts))
    checks = assert_cancellation(sample)
    return BenchmarkSample(**{**sample.__dict__, "passed": passed(checks) and bool(facts["parent_cancelled"]) and bool(facts["child_cancelled"]), "assertions_passed": sum(check.passed for check in checks)})


async def _concurrent_samples(root: Path, attempt: int, warmup: bool, config: DelegationWorkloadConfig) -> list[BenchmarkSample]:
    """将一轮多父并发的每条真实链路记录为独立采样。"""
    facts_list = await run_concurrent_completed(root, config, attempt)
    samples: list[BenchmarkSample] = []
    for index, facts in enumerate(facts_list):
        sample = await _outcome_sample(root, ChildOutcome.COMPLETED, attempt, warmup, config, index, facts)
        samples.append(BenchmarkSample(**{**sample.__dict__, "case_id": "concurrent_isolation"}))
    return samples


def write_artifacts(samples: Sequence[BenchmarkSample], config: DelegationWorkloadConfig, output: Path, baseline: Path | None) -> None:
    """写入候选实验工件；存在未执行样本时拒绝创建正式快照。"""
    output.mkdir(parents=True, exist_ok=True)
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + _commit()
    sample_path = output / "samples" / f"{identifier}.jsonl"
    formal_sample_path = output / "samples" / f"{identifier}.formal.jsonl"
    sample_path.parent.mkdir(exist_ok=True)
    sample_path.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in samples), encoding="utf-8")
    (output / "delegation-config.json").write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    _validate_snapshot_samples(samples, config)
    formal_samples = [item for item in samples if not item.is_warmup]
    formal_sample_path.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in formal_samples), encoding="utf-8")
    outcome = [item for item in samples if item.case_id in {kind.value for kind in ChildOutcome} and not item.is_warmup]
    cancellation = [item for item in samples if item.case_id == "parent_cancellation" and not item.is_warmup]
    concurrent = [item for item in samples if item.case_id == "concurrent_isolation" and not item.is_warmup]
    outcome_summary, cancellation_summary, concurrent_summary = summarize(outcome), summarize(cancellation), summarize(concurrent)
    (output / "outcome-matrix.md").write_text("# PR7 委派终态表\n\n| 子终态 | 通过/总数 | 错误数 |\n|---|---:|---:|\n" + "\n".join(f"| {kind.value} | {sum(item.passed for item in outcome if item.case_id == kind.value)}/{sum(1 for item in outcome if item.case_id == kind.value)} | {sum(not item.passed for item in outcome if item.case_id == kind.value)} |" for kind in ChildOutcome) + "\n", encoding="utf-8")
    def cancellation_latency(field: str) -> tuple[float | None, float | None]:
        values = [getattr(item, field) for item in cancellation if getattr(item, field) is not None]
        return (percentile(values, 50.0), percentile(values, 95.0)) if values else (None, None)
    delivery_p50, delivery_p95 = cancellation_latency("cancel_delivery_ms")
    parent_p50, parent_p95 = cancellation_latency("parent_cancel_effect_ms")
    child_p50, child_p95 = cancellation_latency("child_cancel_effect_ms")
    (output / "cancellation.md").write_text(f"# PR7 父取消传播\n\n正式样本：{cancellation_summary.sample_count}；通过：{cancellation_summary.passed_count}；错误：{cancellation_summary.error_count}。\n\n| 指标 | P50 ms | P95 ms |\n|---|---:|---:|\n| 取消送达 | {delivery_p50} | {delivery_p95} |\n| 父 Run 生效 | {parent_p50} | {parent_p95} |\n| 子 Run 生效 | {child_p50} | {child_p95} |\n", encoding="utf-8")
    (output / "concurrent-isolation.md").write_text(f"# PR7 多父并发隔离\n\n父 Session：{config.concurrent_parents}；正式轮数：{config.concurrent_repeat}；总链路：{concurrent_summary.sample_count}；通过：{concurrent_summary.passed_count}；错误：{concurrent_summary.error_count}；回灌 P50/P95：{concurrent_summary.suspend_to_backfill_p50_ms}/{concurrent_summary.suspend_to_backfill_p95_ms} ms。\n", encoding="utf-8")
    snapshot = build_snapshot(snapshot_id=identifier, generated_at=datetime.now(UTC).isoformat(), git_commit=_commit(),
        dataset=SUITE_DELEGATION, environment={"python_version": sys.version.split()[0], "platform": platform.platform(), "config_hash": samples[0].config_hash, "eval_schema_version": "runtime-v4"},
        warmup=sum(item.is_warmup for item in samples), repeat=sum(not item.is_warmup for item in samples), samples=samples,
        samples_path=f"samples/{identifier}.formal.jsonl", scenario_id=SUITE_DELEGATION, samples_content_summary={"line_count": len(formal_samples), "byte_count": formal_sample_path.stat().st_size})
    snapshot_path = output / f"{identifier}.json"
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if baseline is not None:
        (baseline / "samples").mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample_path, baseline / "samples" / sample_path.name)
        shutil.copy2(formal_sample_path, baseline / "samples" / formal_sample_path.name)
        shutil.copy2(snapshot_path, baseline / snapshot_path.name)


def _validate_snapshot_samples(samples: Sequence[BenchmarkSample], config: DelegationWorkloadConfig) -> None:
    """在快照生成前校验三类场景、样本数与统一证据元数据。"""
    formal = [item for item in samples if not item.is_warmup]
    expected = 4 * config.outcome_repeat + config.cancellation_repeat + config.concurrent_parents * config.concurrent_repeat
    if len(formal) != expected:
        raise ValueError(f"正式样本数不匹配：实际 {len(formal)}，期望 {expected}")
    expected_cases = {kind.value: config.outcome_repeat for kind in ChildOutcome} | {"parent_cancellation": config.cancellation_repeat, "concurrent_isolation": config.concurrent_parents * config.concurrent_repeat}
    for case_id, count in expected_cases.items():
        if sum(item.case_id == case_id and not item.is_warmup for item in samples) != count:
            raise ValueError(f"场景 {case_id} 正式样本数不匹配")
    metadata = {(item.git_commit, item.fixture_version, item.config_hash, tuple(sorted((item.environment or {}).items()))) for item in samples}
    if len(metadata) != 1 or any(item.formal_sampling is not True for item in formal):
        raise ValueError("快照样本的提交、Fixture、配置、环境或正式标识不一致")


def main(argv: Sequence[str] | None = None) -> int:
    """解析 PR7 采样参数；正式执行器接入前仅生成不可资格化的配置工件。"""
    parser = argparse.ArgumentParser(description="PR7 多 Agent 委派可靠性")
    parser.add_argument("--suite", default=SUITE_DELEGATION)
    parser.add_argument("--outcome-warmup", type=int, default=1)
    parser.add_argument("--outcome-repeat", type=int, default=1)
    parser.add_argument("--cancellation-warmup", type=int, default=5)
    parser.add_argument("--cancellation-repeat", type=int, default=50)
    parser.add_argument("--concurrent-parents", type=int, default=8)
    parser.add_argument("--concurrent-warmup", type=int, default=5)
    parser.add_argument("--concurrent-repeat", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--save-baseline", type=Path)
    args = parser.parse_args(argv)
    if args.suite != SUITE_DELEGATION or min(args.outcome_warmup, args.cancellation_warmup, args.concurrent_warmup) < 0 or min(args.outcome_repeat, args.cancellation_repeat, args.concurrent_repeat, args.concurrent_parents) <= 0:
        parser.error("suite 或采样参数不合法")
    config = DelegationWorkloadConfig(concurrent_parents=args.concurrent_parents, outcome_warmup=args.outcome_warmup, outcome_repeat=args.outcome_repeat, cancellation_warmup=args.cancellation_warmup, cancellation_repeat=args.cancellation_repeat, concurrent_warmup=args.concurrent_warmup, concurrent_repeat=args.concurrent_repeat)
    with tempfile.TemporaryDirectory(prefix="dotclaw-delegation-") as directory:
        root = Path(directory)
        samples: list[BenchmarkSample] = []
        for outcome in ChildOutcome:
            for attempt in range(args.outcome_warmup + args.outcome_repeat):
                samples.append(asyncio.run(_outcome_sample(root / f"outcome-{outcome.value}-{attempt}", outcome, attempt, attempt < args.outcome_warmup, config)))
        for attempt in range(args.cancellation_warmup + args.cancellation_repeat):
            samples.append(asyncio.run(_cancellation_sample(root / f"cancellation-{attempt}", attempt, attempt < args.cancellation_warmup, config)))
        for attempt in range(args.concurrent_warmup + args.concurrent_repeat):
            samples.extend(asyncio.run(_concurrent_samples(root / f"concurrent-{attempt}", attempt, attempt < args.concurrent_warmup, config)))
    write_artifacts(samples, config, args.output, args.save_baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
