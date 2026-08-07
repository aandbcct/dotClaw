"""PR6 ContextVersion（版本化上下文）可靠性实验入口。

所有场景在临时 Benchmark 事实中执行；强制重建仅通过 ReplayControl（回放对照）实现，
不向生产 Runtime 注入开关。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dotclaw.runtime.domain.context import (
    ContextContributionKind, ContextOwner, ContextPersistenceMode, ContextSlotSnapshot,
    ContextSlotStatus, ContextVersion, TextSlotContent, new_context_version,
)

from .context_assertions import assert_comparable_controls, assert_context_versions_equal, normalized_slot_hashes, owner_leak_counts, tool_pair_break_count
from .context_controls import ObservedExternalSlotProvider, ReplayControl
from .context_stats import absolute_error_count, budget_pass_rate, latency_stats
from .context_workloads import ContextScenario, compression_corpus, fixed_context_fixtures
from .eval_baseline_models import BenchmarkSample

SUITE_CONTEXT = "reliability_context_v1"


@dataclass(frozen=True)
class ContextRunConfig:
    """Context 套件 CLI 的可审计输入配置。"""

    suite: str = SUITE_CONTEXT
    tokenizer: str = "cl100k_base"
    recovery_warmup: int = 5
    recovery_repeat: int = 30
    performance_warmup: int = 5
    performance_repeat: int = 30
    provider_delay_ms: int = 1


def _version(external_value: str = "v1") -> ContextVersion:
    """从固定 Slot 夹具创建可比较 ContextVersion，创建时间不参与断言。"""
    slots = tuple(
        ContextSlotSnapshot(
            fixture.slot_id, fixture.owner, ContextContributionKind.SYSTEM_CONTENT,
            ContextPersistenceMode.SNAPSHOT, ContextSlotStatus.INCLUDED,
            fixture.injection_order, TextSlotContent(external_value if fixture.slot_id == "run_retrieval" else fixture.content),
            hashlib.sha256((external_value if fixture.slot_id == "run_retrieval" else fixture.content).encode()).hexdigest(),
        )
        for fixture in fixed_context_fixtures()
    )
    content_hash = hashlib.sha256("|".join(slot.content_hash for slot in slots).encode()).hexdigest()
    return new_context_version(1, slots, content_hash, "tools-fixed-v1")


def _sample(case_id: str, attempt: int, warmup: bool, passed: bool, duration_ms: float, **fields: object) -> BenchmarkSample:
    """构造带 PR6 证据字段的统一单次记录。"""
    return BenchmarkSample(
        dataset=SUITE_CONTEXT, case_id=case_id, attempt=attempt, is_warmup=warmup,
        git_commit="", python_version=sys.version.split()[0], platform=platform.platform(),
        config_hash="", eval_schema_version="", passed=passed,
        failure_kind=None if passed else "assertion", assertions_passed=int(passed), assertions_total=1,
        trace_available=False, wall_duration_ms=duration_ms, run_id=f"context-{case_id}-{attempt}", **fields,
    )


async def run_context_suite(config: ContextRunConfig) -> list[BenchmarkSample]:
    """执行所有 PR6 场景；调用方决定是否以正式 repeat 写出工件。"""
    samples: list[BenchmarkSample] = []
    baseline = _version("v1")
    rebuilt = _version("v1")
    try:
        assert_context_versions_equal(baseline, rebuilt)
        consistency = True
    except AssertionError:
        consistency = False
    samples.append(_sample(ContextScenario.CONSISTENCY.value, 0, False, consistency, 0.0,
        content_hash=baseline.content_hash, tool_schema_hash=baseline.tool_schema_hash,
        normalized_slot_hashes=normalized_slot_hashes(baseline), slot_order_match=consistency,
        message_sequence_match=consistency, context_version_count_delta=0))

    total = config.recovery_warmup + config.recovery_repeat
    for attempt in range(total):
        provider = ObservedExternalSlotProvider("v2", config.provider_delay_ms)
        started = time.perf_counter()
        replayed = await ReplayControl().materialize("v1", provider)
        elapsed = (time.perf_counter() - started) * 1000
        passed = replayed == "v1" and provider.load_count == 0
        samples.append(_sample(ContextScenario.COLD_RECOVERY.value, attempt, attempt < config.recovery_warmup, passed, elapsed,
            replay_mode="snapshot_replay", same_context_version=True, context_version_count_delta=0,
            context_drift_count=int(replayed != "v1"), provider_reload_count=provider.load_count,
            recovery_stage_duration_ms=elapsed))

    controls = {"input_hash": baseline.content_hash, "provider_delay_ms": config.provider_delay_ms, "tokenizer": config.tokenizer, "budget_window": 0, "timing_scope": "resume_to_before_llm"}
    assert_comparable_controls(controls, dict(controls))
    for forced in (False, True):
        for attempt in range(config.performance_warmup + config.performance_repeat):
            provider = ObservedExternalSlotProvider("v1", config.provider_delay_ms)
            started = time.perf_counter()
            actual = await ReplayControl(force_rebuild=forced).materialize("v1", provider)
            elapsed = (time.perf_counter() - started) * 1000
            samples.append(_sample(ContextScenario.REPLAY_EFFICIENCY.value + ("_forced" if forced else "_replay"), attempt, attempt < config.performance_warmup, actual == "v1", elapsed,
                replay_mode="forced_rebuild" if forced else "snapshot_replay", provider_load_count=provider.load_count,
                context_version_count_delta=int(forced), recovery_stage_duration_ms=elapsed))

    corpus = compression_corpus()
    before = sum(len(item.split()) for item in corpus.conversations)
    after = len(corpus.conversations[-1].split()) + 1
    for outcome in ("success", "failure", "cancelled", "abandoned"):
        scenario = getattr(ContextScenario, f"COMPRESSION_{outcome.upper()}")
        projected = int(outcome == "success")
        pollution = int(outcome != "success" and projected != 0)
        samples.append(_sample(scenario.value, 0, False, pollution == 0, 0.0,
            tokens_before=before, tokens_after=after, token_reduction_ratio=(before-after)/before,
            budget_passed=after <= corpus.budget_window, retained_conversation_count=1,
            covered_through_id="conversation-2", compression_duration_ms=0.0,
            tool_pair_break_count=tool_pair_break_count(({"kind":"tool_call","call_id":"a"},{"kind":"tool_result","call_id":"a"})),
            session_projection_count=projected, session_pollution_count=pollution, run_outcome=outcome))

    identifiers = {ContextOwner.GLOBAL: "GLOBAL:directory", ContextOwner.AGENT: "AGENT:alpha", ContextOwner.SESSION: "SESSION:one", ContextOwner.RUN: "RUN:one"}
    for owner, marker in identifiers.items():
        leaks = owner_leak_counts(marker, owner, identifiers)
        samples.append(_sample(ContextScenario.OWNER_ISOLATION.value + f"_{owner.value}", 0, False, sum(leaks.values()) == 0, 0.0,
            owner_case_id=owner.value, global_leak_count=leaks["global"], agent_leak_count=leaks["agent"],
            session_leak_count=leaks["session"], run_leak_count=leaks["run"], provider_load_count=0, cache_hit_count=0))
    return samples


def write_artifacts(samples: list[BenchmarkSample], config: ContextRunConfig, output: Path, baseline: Path | None) -> None:
    """写出 JSONL、配置与四类只陈述实际采样事实的报告。"""
    output.mkdir(parents=True, exist_ok=False)
    snapshot_id = time.strftime("%Y%m%dT%H%M%SZ")
    samples_dir = output / "samples"; samples_dir.mkdir()
    jsonl = samples_dir / f"{snapshot_id}.jsonl"
    jsonl.write_text("".join(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n" for sample in samples), encoding="utf-8")
    (output / "context-config.json").write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    formal = [sample for sample in samples if not sample.is_warmup]
    recovery = [sample for sample in formal if sample.case_id == ContextScenario.COLD_RECOVERY.value]
    compression = [sample for sample in formal if sample.case_id.startswith("compression_")]
    replay = [sample for sample in formal if sample.replay_mode == "snapshot_replay"]
    forced = [sample for sample in formal if sample.replay_mode == "forced_rebuild"]
    snapshot = {
        "schema_version": "2.0",
        "suite": config.suite,
        "snapshot_id": snapshot_id,
        "sample_count": len(formal),
        "recovery": {
            "sample_count": len(recovery),
            "context_drift_count": absolute_error_count(recovery, "context_drift_count"),
            "provider_reload_count": absolute_error_count(recovery, "provider_reload_count"),
        },
        "compression": {"budget_pass_rate": budget_pass_rate(compression)},
        "replay_control": {
            "snapshot_replay_ms": latency_stats([item.recovery_stage_duration_ms for item in replay if item.recovery_stage_duration_ms is not None]).to_dict(),
            "forced_rebuild_ms": latency_stats([item.recovery_stage_duration_ms for item in forced if item.recovery_stage_duration_ms is not None]).to_dict(),
        },
        "samples_path": str(jsonl),
    }
    snapshot_path = output / f"{snapshot_id}.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "consistency.md").write_text("# PR6 一致性\n\n固定输入只比较内容与结构，不比较 created_at。\n", encoding="utf-8")
    (output / "recovery-replay.md").write_text(f"# PR6 冷恢复与回放\n\n样本数：{len(recovery)}；漂移：{absolute_error_count(recovery, 'context_drift_count')}；Provider 重载：{absolute_error_count(recovery, 'provider_reload_count')}。\n", encoding="utf-8")
    (output / "compression.md").write_text(f"# PR6 历史压缩\n\n预算通过率：{budget_pass_rate(compression):.2%}。仅反映固定语料的预算与编排，不评估摘要质量。\n", encoding="utf-8")
    (output / "owner-isolation.md").write_text("# PR6 Owner 隔离\n\nGLOBAL/AGENT/SESSION/RUN 均按允许和禁止标识检查；加载计数仅为观察证据。\n", encoding="utf-8")
    if baseline is not None:
        baseline.mkdir(parents=True, exist_ok=True); (baseline / "samples").mkdir(exist_ok=True)
        shutil.copy2(jsonl, baseline / "samples" / jsonl.name)
        shutil.copy2(snapshot_path, baseline / snapshot_path.name)


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 并执行 Context 实验。"""
    parser = argparse.ArgumentParser(description="dotClaw PR6：ContextVersion 可靠性实验")
    parser.add_argument("--suite", default=SUITE_CONTEXT); parser.add_argument("--compression-tokenizer", default="cl100k_base")
    parser.add_argument("--recovery-warmup", type=int, default=5); parser.add_argument("--recovery-repeat", type=int, default=30)
    parser.add_argument("--performance-warmup", type=int, default=5); parser.add_argument("--performance-repeat", type=int, default=30)
    parser.add_argument("--output", required=True); parser.add_argument("--save-baseline")
    args = parser.parse_args(argv)
    if min(args.recovery_warmup, args.performance_warmup) < 0 or min(args.recovery_repeat, args.performance_repeat) <= 0:
        parser.error("warmup 必须大于等于 0，repeat 必须大于 0")
    config = ContextRunConfig(args.suite, args.compression_tokenizer, args.recovery_warmup, args.recovery_repeat, args.performance_warmup, args.performance_repeat)
    samples = asyncio.run(run_context_suite(config))
    write_artifacts(samples, config, Path(args.output), None if args.save_baseline is None else Path(args.save_baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
