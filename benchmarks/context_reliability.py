"""PR6 ContextVersion（版本化上下文）可靠性实验入口。"""

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
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.application.request_factory import create_run_request
from dotclaw.context import ContextDependencies, build_context_provider
from dotclaw.context.ports import UserProfile

from .context_assertions import assert_comparable_controls, assert_context_versions_equal, normalized_slot_hashes, owner_leak_counts, tool_pair_break_count
from .context_runtime_fixture import BenchmarkAgentDirectory, BenchmarkCompactor, BlockingLLM, CapturingLLM, CompletingTool, FixedTokenizerCounter, ObservedKnowledgeBase, ToolThenFinalLLM, build_engine, session_with_history
from .context_stats import absolute_error_count, budget_pass_rate, latency_stats, wilson_interval
from .context_workloads import ContextScenario, compression_corpus
from .eval_baseline_models import BENCHMARK_SCHEMA_VERSION, BenchmarkSample

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
    boundary_warmup: int = 0
    boundary_repeat: int = 30
    provider_delay_ms: int = 1


def _sample(case_id: str, attempt: int, warmup: bool, passed: bool, duration_ms: float, **fields: object) -> BenchmarkSample:
    """构造带 PR6 证据字段的统一单次记录。"""
    config_fingerprint = hashlib.sha256(json.dumps(asdict(_ACTIVE_CONFIG), sort_keys=True).encode("utf-8")).hexdigest()
    return BenchmarkSample(dataset=SUITE_CONTEXT, case_id=case_id, attempt=attempt, is_warmup=warmup, git_commit=_git_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash=config_fingerprint, eval_schema_version=BENCHMARK_SCHEMA_VERSION, passed=passed, failure_kind=None if passed else "assertion", assertions_passed=int(passed), assertions_total=1, trace_available=False, wall_duration_ms=duration_ms, run_id=f"context-{case_id}-{attempt}", **fields)


_ACTIVE_CONFIG: ContextRunConfig = ContextRunConfig()


def _git_commit() -> str:
    """读取当前提交；无 Git 事实时拒绝伪造可追溯证据。"""
    return subprocess.run(("git", "rev-parse", "HEAD"), check=True, capture_output=True, text=True).stdout.strip()


def _messages(bundle_messages: object) -> tuple[tuple[str, str], ...]:
    """提取实际送入 LLM 的角色与正文序列。"""
    return tuple((message.role.value, message.content) for message in bundle_messages)  # type: ignore[union-attr]


def _control_fingerprint(config: ContextRunConfig, input_hash: str, mode: str) -> dict[str, object]:
    """为两侧独立装配的恢复对照生成可比较性事实。"""
    return {"input_hash": input_hash, "provider_delay_ms": config.provider_delay_ms, "tokenizer": config.tokenizer, "budget_window": 200, "timing_scope": "resume_to_first_llm", "mode": mode}


async def _cold_recovery(config: ContextRunConfig, attempt: int, warmup: bool) -> BenchmarkSample:
    """通过真实 checkpoint、服务销毁和公开 resume_run 验证 v1 快照恢复。"""
    root = Path(tempfile.mkdtemp(prefix="dotclaw-pr6-recovery-"))
    manager, session, request = await session_with_history(root)
    initial_source = ObservedKnowledgeBase("RUN:v1", config.provider_delay_ms)
    initial_llm = CapturingLLM(unavailable_first=True)
    engine, repository = build_engine(root, manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), initial_llm, initial_source)
    interrupted = await engine.execute(request)
    before = await repository.load_context_versions(session.id, interrupted.run_id)
    before_messages = await repository.load_messages(session.id, interrupted.run_id)
    before_events = await repository.load_events(session.id, interrupted.run_id)
    before_session = await manager.load(session.id)
    # 新对象使用 v2 来源；若 replay 生效，该对象不会被生产 ContextProvider 调用。
    resumed_source = ObservedKnowledgeBase("RUN:v2", config.provider_delay_ms)
    resumed_llm = CapturingLLM()
    resumed_engine, resumed_repository = build_engine(root, manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), resumed_llm, resumed_source)
    started = time.perf_counter()
    resumed = await resumed_engine.resume_run(interrupted.run_id)
    elapsed = (time.perf_counter() - started) * 1000
    after = await resumed_repository.load_context_versions(session.id, interrupted.run_id)
    after_messages = await resumed_repository.load_messages(session.id, interrupted.run_id)
    after_events = await resumed_repository.load_events(session.id, interrupted.run_id)
    after_session = await manager.load(session.id)
    actual = _messages(resumed_llm.contexts[-1].messages) if resumed_llm.contexts else ()
    actual_text = "\n".join(content for _, content in actual)
    same = len(before) == len(after) == 1 and before[0] == after[0]
    conversation_delta = len(after_session.conversations) - len(before_session.conversations) if after_session is not None and before_session is not None else -1
    message_delta, event_delta = len(after_messages) - len(before_messages), len(after_events) - len(before_events)
    passed = resumed.state.outcome() is not None and same and "RUN:v1" in actual_text and "RUN:v2" not in actual_text and resumed_source.load_count == 0 and conversation_delta == 1 and message_delta == 1 and event_delta > 0
    return _sample(ContextScenario.COLD_RECOVERY.value, attempt, warmup, passed, elapsed, replay_mode="snapshot_replay", same_context_version=same, context_version_count_delta=len(after) - len(before), context_drift_count=int("RUN:v2" in actual_text or "RUN:v1" not in actual_text), provider_reload_count=resumed_source.load_count, recovery_stage_duration_ms=elapsed, conversation_count_delta=conversation_delta, run_message_count_delta=message_delta, run_event_count_delta=event_delta)


async def _replay_control(config: ContextRunConfig, attempt: int, warmup: bool, forced: bool) -> BenchmarkSample:
    """在相同真实恢复节点比较快照重放与 Provider 重建。"""
    root = Path(tempfile.mkdtemp(prefix="dotclaw-pr6-control-"))
    manager, session, request = await session_with_history(root)
    engine, repository = build_engine(root, manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), CapturingLLM(unavailable_first=True), ObservedKnowledgeBase("RUN:v1", config.provider_delay_ms))
    interrupted = await engine.execute(request)
    before = await repository.load_context_versions(session.id, interrupted.run_id)
    source = ObservedKnowledgeBase("RUN:v1", config.provider_delay_ms)
    llm = CapturingLLM()
    resumed_engine, resumed_repository = build_engine(root, manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), llm, source, force_rebuild=forced)
    started = time.perf_counter()
    resumed = await resumed_engine.resume_run(interrupted.run_id)
    elapsed = (time.perf_counter() - started) * 1000
    after = await resumed_repository.load_context_versions(session.id, interrupted.run_id)
    expected_loads = 1 if forced else 0
    passed = resumed.state.outcome() is not None and source.load_count == expected_loads
    return _sample(ContextScenario.REPLAY_EFFICIENCY.value + ("_forced" if forced else "_replay"), attempt, warmup, passed, elapsed, replay_mode="forced_rebuild" if forced else "snapshot_replay", provider_load_count=source.load_count, context_version_count_delta=len(after) - len(before), recovery_stage_duration_ms=elapsed)


async def _compression(config: ContextRunConfig) -> list[BenchmarkSample]:
    """执行真实成功、失败、取消、放弃 Run，并从 Session/ContextVersion 读取投影事实。"""
    samples: list[BenchmarkSample] = []
    for outcome in ("success", "failure", "cancelled", "abandoned"):
        for attempt in range(config.boundary_warmup + config.boundary_repeat):
            baseline_root = Path(tempfile.mkdtemp(prefix=f"dotclaw-pr6-compression-disabled-{outcome}-"))
            baseline_manager, baseline_session, baseline_request = await session_with_history(baseline_root, 3)
            baseline_counter = FixedTokenizerCounter(config.tokenizer)
            baseline_engine, _ = build_engine(baseline_root, baseline_manager, baseline_counter, BenchmarkCompactor(), CapturingLLM(), context_window=200)
            baseline_result = await baseline_engine.execute(baseline_request)
            baseline_tokens = (await baseline_counter.count(baseline_counter.requests[0])).input_tokens
            samples.append(_sample(f"compression_without_compression_{outcome}", attempt, attempt < config.boundary_warmup, baseline_result.state.outcome() is not None, 0.0, replay_mode="compression_disabled", tokens_before=baseline_tokens, tokens_after=baseline_tokens, token_reduction_ratio=0.0, budget_passed=baseline_tokens <= 70, retained_conversation_count=3, tool_pair_break_count=0, run_outcome=outcome))
            root = Path(tempfile.mkdtemp(prefix=f"dotclaw-pr6-compression-{outcome}-"))
            manager, session, request = await session_with_history(root, 3)
            counter, compactor = FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor()
            llm = BlockingLLM() if outcome == "cancelled" else ToolThenFinalLLM() if outcome == "success" else CapturingLLM(unavailable_first=outcome == "abandoned", fail_first=outcome == "failure")
            engine, repository = build_engine(root, manager, counter, compactor, llm, context_window=70, tool=CompletingTool() if outcome == "success" else None)
            compression_started = time.perf_counter()
            if outcome == "cancelled":
                task = asyncio.create_task(engine.execute(request)); await llm.started.wait(); await engine.cancel(llm.run_id, "PR6 取消"); result = await task
            else:
                result = await engine.execute(request)
            if outcome == "abandoned":
                result = await engine.abandon_run(result.run_id)
            persisted = await manager.load(session.id); versions = await repository.load_context_versions(session.id, result.run_id)
            before, after = counter.requests[0], counter.requests[-1]
            active = persisted.active_history_compression() if persisted is not None else None
            projected, pollution = int(active is not None), int(outcome != "success" and active is not None)
            token_before, token_after = (await counter.count(before)).input_tokens, (await counter.count(after)).input_tokens
            expected_state = {"success": "completed", "failure": "failed", "cancelled": "cancelled", "abandoned": "abandoned"}[outcome]
            actual_state = result.state.outcome().value if result.state.outcome() is not None else ""
            next_request_ok = True
            if outcome == "success" and persisted is not None:
                next_llm = CapturingLLM()
                next_engine, _ = build_engine(root, manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), next_llm, context_window=200)
                await next_engine.execute(create_run_request(persisted, session.agent_id, "下一次请求"))
                next_text = "\n".join(content for _, content in _messages(next_llm.contexts[0].messages))
                next_request_ok = "PR6 固定摘要" in next_text and "旧问题-2" in next_text
            tool_messages = await repository.load_messages(session.id, result.run_id)
            tool_facts = tuple({"kind": "tool_call" if message.tool_calls else "tool_result" if message.kind.value == "tool_result" else "other", "call_id": message.tool_call_id or (message.tool_calls[0].call_id if message.tool_calls else "")} for message in tool_messages)
            tool_breaks = tool_pair_break_count(tool_facts)
            passed = bool(compactor.requests) and actual_state == expected_state and next_request_ok and tool_breaks == 0 and (outcome != "success" or projected == 1) and (outcome == "success" or pollution == 0)
            duration = (time.perf_counter() - compression_started) * 1000
            samples.append(_sample(getattr(ContextScenario, f"COMPRESSION_{outcome.upper()}").value, attempt, attempt < config.boundary_warmup, passed, duration, replay_mode="compression_enabled", tokens_before=token_before, tokens_after=token_after, token_reduction_ratio=(token_before - token_after) / token_before, budget_passed=token_after <= 70, retained_conversation_count=len(after.history_messages) // 2, covered_through_id=active.covered_through_conversation_id if active is not None else None, compression_duration_ms=duration, tool_pair_break_count=tool_breaks, session_projection_count=projected, session_pollution_count=pollution, run_outcome=outcome, context_version_count_delta=len(versions)))
    return samples


async def _owner_samples(config: ContextRunConfig) -> list[BenchmarkSample]:
    """以不同 Agent、Session、Run 的真实 ContextVersion 互相检查禁止标识。"""
    identifiers = {ContextOwner.GLOBAL: "GLOBAL", ContextOwner.AGENT: "AGENT:alpha", ContextOwner.SESSION: "SESSION:one", ContextOwner.RUN: "RUN:one"}
    forbidden = ("AGENT:beta", "RUN:two")
    root = Path(tempfile.mkdtemp(prefix="dotclaw-pr6-owner-"))
    shared_source = ObservedKnowledgeBase("RUN:one")
    shared_provider = build_context_provider(ContextDependencies(knowledge_base=shared_source, user_profile=UserProfile(name="SESSION:one"), agent_registry=BenchmarkAgentDirectory()))
    manager, session, request = await session_with_history(root, agent_id="alpha", session_marker="HISTORY")
    engine, repository = build_engine(root, manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), CapturingLLM(), context_port=shared_provider)
    result = await engine.execute(request)
    version = (await repository.load_context_versions(session.id, result.run_id))[0]
    shared_source.value = "RUN:two"
    other_manager, other_session, other_request = await session_with_history(root, agent_id="beta", session_marker="HISTORY")
    other_engine, other_repository = build_engine(root, other_manager, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), CapturingLLM(), context_port=shared_provider)
    other_result = await other_engine.execute(other_request)
    other_version = (await other_repository.load_context_versions(other_session.id, other_result.run_id))[0]
    visible_by_owner: dict[ContextOwner, str] = {}
    for slot in version.slots:
        visible_by_owner[slot.owner] = visible_by_owner.get(slot.owner, "") + json.dumps(slot.to_dict(), ensure_ascii=False)
    other_text = json.dumps([slot.to_dict() for slot in other_version.slots], ensure_ascii=False)
    samples: list[BenchmarkSample] = []
    for owner, marker in identifiers.items():
        visible = visible_by_owner.get(owner, "")
        leaks = owner_leak_counts(visible, owner, identifiers)
        cross_leaks = sum(marker_text in visible for marker_text in forbidden)
        allowed = marker in visible and "AGENT:alpha" in json.dumps([slot.to_dict() for slot in version.slots], ensure_ascii=False) and all(marker_text in other_text for marker_text in forbidden)
        samples.append(_sample(ContextScenario.OWNER_ISOLATION.value + f"_{owner.value}", 0, False, allowed and sum(leaks.values()) == 0 and cross_leaks == 0, 0.0, owner_case_id=owner.value, global_leak_count=leaks["global"], agent_leak_count=leaks["agent"], session_leak_count=leaks["session"], run_leak_count=leaks["run"], provider_load_count=shared_source.load_count, cache_hit_count=0))
    return samples


async def run_context_suite(config: ContextRunConfig) -> list[BenchmarkSample]:
    """执行所有 PR6 场景；正式重复由 CLI 参数明确决定。"""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = config
    samples: list[BenchmarkSample] = []
    root_one, root_two = Path(tempfile.mkdtemp(prefix="dotclaw-pr6-consistency-")), Path(tempfile.mkdtemp(prefix="dotclaw-pr6-consistency-"))
    manager_one, session_one, request_one = await session_with_history(root_one)
    llm_one = CapturingLLM(); engine_one, repository_one = build_engine(root_one, manager_one, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), llm_one, ObservedKnowledgeBase("RUN:one"))
    result_one = await engine_one.execute(request_one)
    manager_two, session_two, request_two = await session_with_history(root_two)
    llm_two = CapturingLLM(); engine_two, repository_two = build_engine(root_two, manager_two, FixedTokenizerCounter(config.tokenizer), BenchmarkCompactor(), llm_two, ObservedKnowledgeBase("RUN:one"))
    result_two = await engine_two.execute(request_two)
    baseline, rebuilt = (await repository_one.load_context_versions(session_one.id, result_one.run_id))[0], (await repository_two.load_context_versions(session_two.id, result_two.run_id))[0]
    try:
        assert_context_versions_equal(baseline, rebuilt)
        consistency = _messages(llm_one.contexts[-1].messages) == _messages(llm_two.contexts[-1].messages)
    except AssertionError:
        consistency = False
    samples.append(_sample(ContextScenario.CONSISTENCY.value, 0, False, consistency, 0.0, content_hash=baseline.content_hash, tool_schema_hash=baseline.tool_schema_hash, normalized_slot_hashes=normalized_slot_hashes(baseline), slot_order_match=consistency, message_sequence_match=consistency, context_version_count_delta=0))
    for attempt in range(config.recovery_warmup + config.recovery_repeat):
        samples.append(await _cold_recovery(config, attempt, attempt < config.recovery_warmup))
    assert_comparable_controls(_control_fingerprint(config, baseline.content_hash, "snapshot_replay"), _control_fingerprint(config, baseline.content_hash, "forced_rebuild"))
    for forced in (False, True):
        for attempt in range(config.performance_warmup + config.performance_repeat):
            samples.append(await _replay_control(config, attempt, attempt < config.performance_warmup, forced))
    samples.extend(await _compression(config))
    samples.extend(await _owner_samples(config))
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
    compression = [sample for sample in formal if sample.replay_mode == "compression_enabled"]
    compression_disabled = [sample for sample in formal if sample.replay_mode == "compression_disabled"]
    replay = [sample for sample in formal if sample.case_id.endswith("_replay")]
    forced = [sample for sample in formal if sample.case_id.endswith("_forced")]
    compression_before = [item.tokens_before for item in compression if item.tokens_before is not None]
    compression_after = [item.tokens_after for item in compression if item.tokens_after is not None]
    compression_ratio = [item.token_reduction_ratio for item in compression if item.token_reduction_ratio is not None]
    baseline_budget_pass_rate = budget_pass_rate(compression_disabled)
    recovery_errors = absolute_error_count(recovery, "context_drift_count") + absolute_error_count(recovery, "provider_reload_count")
    recovery_wilson = wilson_interval(recovery_errors, len(recovery))
    snapshot = {"schema_version": BENCHMARK_SCHEMA_VERSION, "suite": config.suite, "snapshot_id": snapshot_id, "git_commit": formal[0].git_commit if formal else "", "config_hash": formal[0].config_hash if formal else "", "sample_count": len(formal), "recovery": {"sample_count": len(recovery), "context_drift_count": absolute_error_count(recovery, "context_drift_count"), "provider_reload_count": absolute_error_count(recovery, "provider_reload_count"), "conversation_count_delta": [item.conversation_count_delta for item in recovery], "run_message_count_delta": [item.run_message_count_delta for item in recovery], "run_event_count_delta": [item.run_event_count_delta for item in recovery]}, "compression": {"budget_pass_rate_without_compression": baseline_budget_pass_rate, "budget_pass_rate_with_compression": budget_pass_rate(compression), "tokens_before": compression_before, "tokens_after": compression_after, "token_reduction_ratio": compression_ratio, "retained_conversation_count": [item.retained_conversation_count for item in compression], "tool_pair_break_count": absolute_error_count(compression, "tool_pair_break_count"), "compression_ms": latency_stats([item.compression_duration_ms for item in compression if item.compression_duration_ms is not None]).to_dict()}, "replay_control": {"snapshot_replay_ms": latency_stats([item.recovery_stage_duration_ms for item in replay if item.recovery_stage_duration_ms is not None]).to_dict(), "forced_rebuild_ms": latency_stats([item.recovery_stage_duration_ms for item in forced if item.recovery_stage_duration_ms is not None]).to_dict(), "snapshot_provider_load_count": absolute_error_count(replay, "provider_load_count"), "forced_provider_load_count": absolute_error_count(forced, "provider_load_count"), "snapshot_context_version_delta": absolute_error_count(replay, "context_version_count_delta"), "forced_context_version_delta": absolute_error_count(forced, "context_version_count_delta")}, "samples_path": f"samples/{jsonl.name}"}
    snapshot_path = output / f"{snapshot_id}.json"; snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    owner = [item for item in formal if item.case_id.startswith(ContextScenario.OWNER_ISOLATION.value)]
    owner_rows = "\n".join(f"| {item.owner_case_id} | {'通过' if item.passed else '失败'} | {item.global_leak_count}/{item.agent_leak_count}/{item.session_leak_count}/{item.run_leak_count} |" for item in owner)
    for name, text in (("consistency.md", "# PR6 一致性\n\n结论来自生产 ContextProvider 与实际 LLM 消息。\n"), ("recovery-replay.md", f"# PR6 冷恢复与回放\n\n样本数：{len(recovery)}；漂移：{absolute_error_count(recovery, 'context_drift_count')}；Provider 重载：{absolute_error_count(recovery, 'provider_reload_count')}；错误率 Wilson 95% 区间：[{recovery_wilson[0]:.2%}, {recovery_wilson[1]:.2%}]。\n"), ("compression.md", f"# PR6 历史压缩\n\nToken 前后：{compression_before} → {compression_after}；缩减率：{compression_ratio}；预算通过率：{baseline_budget_pass_rate:.2%} → {budget_pass_rate(compression):.2%}；保留 Conversation：{[item.retained_conversation_count for item in compression]}；工具边界错误：{absolute_error_count(compression, 'tool_pair_break_count')}；完整压缩编排 P50/P95：{snapshot['compression']['compression_ms']['p50_ms']:.2f}/{snapshot['compression']['compression_ms']['p95_ms']:.2f}ms。\n"), ("owner-isolation.md", f"# PR6 Owner 隔离\n\n| Owner | 通过 | GLOBAL/AGENT/SESSION/RUN 泄漏 |\n| --- | --- | --- |\n{owner_rows}\n")):
        (output / name).write_text(text, encoding="utf-8")
    if baseline is not None:
        baseline.mkdir(parents=True, exist_ok=True); (baseline / "samples").mkdir(exist_ok=True)
        shutil.copy2(jsonl, baseline / "samples" / jsonl.name); shutil.copy2(snapshot_path, baseline / snapshot_path.name)


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 并执行 Context 实验。"""
    parser = argparse.ArgumentParser(description="dotClaw PR6：ContextVersion 可靠性实验")
    parser.add_argument("--suite", default=SUITE_CONTEXT); parser.add_argument("--compression-tokenizer", default="cl100k_base")
    parser.add_argument("--recovery-warmup", type=int, default=5); parser.add_argument("--recovery-repeat", type=int, default=30); parser.add_argument("--performance-warmup", type=int, default=5); parser.add_argument("--performance-repeat", type=int, default=30); parser.add_argument("--boundary-warmup", type=int, default=0); parser.add_argument("--boundary-repeat", type=int, default=30); parser.add_argument("--output", required=True); parser.add_argument("--save-baseline")
    args = parser.parse_args(argv)
    if min(args.recovery_warmup, args.performance_warmup, args.boundary_warmup) < 0 or min(args.recovery_repeat, args.performance_repeat, args.boundary_repeat) <= 0:
        parser.error("warmup 必须大于等于 0，repeat 必须大于 0")
    config = ContextRunConfig(args.suite, args.compression_tokenizer, args.recovery_warmup, args.recovery_repeat, args.performance_warmup, args.performance_repeat, args.boundary_warmup, args.boundary_repeat)
    write_artifacts(asyncio.run(run_context_suite(config)), config, Path(args.output), None if args.save_baseline is None else Path(args.save_baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
