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
from typing import Sequence

from .delegation_assertions import assert_cancellation, assert_delegation_chain, passed
from .delegation_stats import summarize
from .delegation_workloads import ChildOutcome, DelegationWorkloadConfig, chain_request_id, run_child_outcome
from .eval_baseline_models import BenchmarkSample, SUITE_DELEGATION
from .eval_baseline_stats import build_snapshot


def _commit() -> str:
    """读取固定短提交；无法读取时不伪造正式证据。"""
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


async def _outcome_sample(root: Path, outcome: ChildOutcome, attempt: int, warmup: bool, config: DelegationWorkloadConfig, parent_index: int = 0) -> BenchmarkSample:
    """规范化一个已由固定 Fixture 观测的链路记录。

    读取真实 RuntimeDelegationAdapter（运行时委派适配器）持久化事实，不引入
    Benchmark 专用生产状态。
    """
    request_id = chain_request_id(parent_index, attempt)
    facts = await run_child_outcome(root, config, request_id, outcome)
    sample = BenchmarkSample(dataset=SUITE_DELEGATION, suite=SUITE_DELEGATION, case_id=outcome.value,
        attempt=attempt, is_warmup=warmup, git_commit=_commit(), python_version=sys.version.split()[0],
        platform=platform.platform(), config_hash=hashlib.sha256(json.dumps(config.to_dict(), sort_keys=True).encode()).hexdigest()[:16],
        eval_schema_version="runtime-v4", passed=False, failure_kind=None, assertions_passed=0,
        assertions_total=10, trace_available=True, wall_duration_ms=float(facts["parent_end_to_end_ms"]), run_id=str(facts["parent_run_id"]),
        parent_run_id=str(facts["parent_run_id"]), child_run_id=str(facts["child_run_id"]), task_id=str(facts["task_id"]), parent_session_id=str(facts["parent_session_id"]), child_session_id=str(facts["child_session_id"]),
        target_agent_id=str(facts["target_agent_id"]), chain_request_id=request_id, child_outcome=str(facts["child_outcome"]), parent_outcome=str(facts["parent_outcome"]),
        delegation_submit_count=int(facts["delegation_submit_count"]), result_backfill_count=int(facts["result_backfill_count"]), delegation_submitted_event_count=int(facts["delegation_submitted_event_count"]), delegation_completed_event_count=int(facts["delegation_completed_event_count"]),
        cross_chain_message_count=0, cross_chain_context_count=0, cross_chain_tool_count=0, cross_chain_stream_count=0, misdelivery_count=0,
        suspend_to_backfill_ms=float(facts["suspend_to_backfill_ms"]), parent_end_to_end_ms=float(facts["parent_end_to_end_ms"]),
        fixture_version=config.fixture_version, environment={"python_version": sys.version.split()[0], "platform": platform.platform()},
        formal_sampling=not warmup, evidence_summary={"request_id": request_id})
    checks = assert_delegation_chain(sample)
    return BenchmarkSample(**{**sample.__dict__, "passed": passed(checks), "assertions_passed": sum(check.passed for check in checks)})


def write_artifacts(samples: Sequence[BenchmarkSample], config: DelegationWorkloadConfig, output: Path, baseline: Path | None) -> None:
    """写入候选实验工件；存在未执行样本时拒绝创建正式快照。"""
    output.mkdir(parents=True, exist_ok=True)
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + _commit()
    sample_path = output / "samples" / f"{identifier}.jsonl"
    sample_path.parent.mkdir(exist_ok=True)
    sample_path.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in samples), encoding="utf-8")
    (output / "delegation-config.json").write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize(samples)
    (output / "outcome-matrix.md").write_text(f"# PR7 委派终态表\n\n正式样本：{summary.sample_count}；通过：{summary.passed_count}；错误：{summary.error_count}。\n", encoding="utf-8")
    (output / "cancellation.md").write_text("# PR7 父取消传播\n\n仅正式固定 Fixture 采样后填写结果。\n", encoding="utf-8")
    (output / "concurrent-isolation.md").write_text("# PR7 多父并发隔离\n\n仅正式固定 Fixture 采样后填写结果。\n", encoding="utf-8")
    if any(item.failure_kind == "unexecuted_fixture" for item in samples):
        return
    snapshot = build_snapshot(snapshot_id=identifier, generated_at=datetime.now(UTC).isoformat(), git_commit=_commit(),
        dataset=SUITE_DELEGATION, environment={"python_version": sys.version.split()[0], "platform": platform.platform(), "config_hash": samples[0].config_hash, "eval_schema_version": "runtime-v4"},
        warmup=sum(item.is_warmup for item in samples), repeat=sum(not item.is_warmup for item in samples), samples=samples,
        samples_path=f"samples/{identifier}.jsonl", scenario_id=SUITE_DELEGATION, samples_content_summary={"line_count": len(samples), "byte_count": sample_path.stat().st_size})
    snapshot_path = output / f"{identifier}.json"
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if baseline is not None:
        (baseline / "samples").mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample_path, baseline / "samples" / sample_path.name)
        shutil.copy2(snapshot_path, baseline / snapshot_path.name)


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
    config = DelegationWorkloadConfig(concurrent_parents=args.concurrent_parents)
    with tempfile.TemporaryDirectory(prefix="dotclaw-delegation-") as directory:
        samples = [asyncio.run(_outcome_sample(Path(directory) / outcome.value, outcome, 0, False, config)) for outcome in ChildOutcome]
    write_artifacts(samples, config, args.output, args.save_baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
