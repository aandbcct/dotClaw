"""PR8 Agent Harness Full/Baseline 配对执行、Judge 与工件写出。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .business_judge import JudgePort, JudgeProtocolError, JudgeSpec, parse_verdict, prompt_hash, redact_review_text, render_prompt
from .eval_baseline import config_hash, git_short_commit, make_snapshot_id, write_jsonl
from .eval_baseline_models import BenchmarkSample, BenchmarkSnapshot
from .eval_baseline_stats import build_snapshot
from .harness_business_dataset import ExecutionCondition, HarnessDataset, HarnessTaskInstance
from .harness_business_report import summarize_harness_business, write_harness_business_report


class HarnessBusinessRunError(ValueError):
    """执行矩阵、确定性观察或正式资格不满足冻结契约。"""


_HARNESS_WORKFLOW_VERSION = "2"


@dataclass(frozen=True)
class HarnessExecutionResult:
    """一次 Runtime 外围执行的最小可评分结果。"""

    candidate: str
    deterministic_passed: bool
    observed_checks: frozenset[str]
    run_id: str | None
    trace_available: bool
    wall_duration_ms: float
    llm_call_count: int
    tool_call_count: int
    failure_kind: str | None = None
    failure_attribution: str | None = None
    evidence_summary: Mapping[str, object] | None = None


class HarnessTaskExecutor(Protocol):
    """按执行条件运行同一任务实例的外围端口。"""

    async def execute(
        self,
        instance: HarnessTaskInstance,
        condition: ExecutionCondition,
        *,
        attempt: int,
        preflight: bool,
    ) -> HarnessExecutionResult:
        """执行一次任务，并返回来自 Runtime/Trace 的确定性观察。"""


async def run_harness_business_matrix(
    dataset: HarnessDataset,
    executor: HarnessTaskExecutor,
    judge: JudgePort,
    *,
    output: Path,
    provider: str,
    model: str,
    temperature: float | None,
    judge_provider: str,
    judge_model: str,
    formal_sampling: bool,
    repeat: int = 3,
    instance_ids: Sequence[str] | None = None,
    timeout_seconds: float | None = None,
    retry_count: int | None = None,
) -> tuple[BenchmarkSnapshot, BenchmarkSnapshot]:
    """运行 Full 与匹配 Baseline；正式模式要求完整 60×3×2 矩阵。"""
    selected = _select_instances(dataset, instance_ids)
    if formal_sampling and (repeat != dataset.formal_repeat or len(selected) != len(dataset.instances)):
        raise HarnessBusinessRunError("正式采样必须覆盖全部 60 个实例且每实例 repeat=3")
    if repeat <= 0:
        raise HarnessBusinessRunError("repeat 必须大于零")
    if formal_sampling and (timeout_seconds is None or retry_count is None):
        raise HarnessBusinessRunError("正式采样必须显式记录 timeout_seconds 与 retry_count")
    if not all((provider, model, judge_provider, judge_model)):
        raise HarnessBusinessRunError("候选模型与 Judge 的 Provider/模型必须显式记录")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError("Agent Harness 输出目录必须为空，拒绝覆盖既有工件")

    preflight = await _run_preflights(dataset, selected, executor)
    (output / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8")
    if formal_sampling and any(not item["runtime_reached"] or not item["trace_available"] for item in preflight):
        raise HarnessBusinessRunError("正式采样 Preflight 必须在每种执行条件下到达 Runtime 且生成 Trace")
    samples: list[BenchmarkSample] = []
    commit = git_short_commit()
    configuration_hash = config_hash()
    for instance in selected:
        baseline = dataset.baseline_for(instance.family)
        for condition in (ExecutionCondition.FULL, baseline):
            for attempt in range(repeat):
                samples.append(
                    await _run_sample(
                        dataset,
                        instance,
                        condition,
                        attempt,
                        executor,
                        judge,
                        provider=provider,
                        model=model,
                        temperature=temperature,
                        judge_provider=judge_provider,
                        judge_model=judge_model,
                        formal_sampling=formal_sampling,
                        commit=commit,
                        configuration_hash=configuration_hash,
                    )
                )

    snapshot_id = make_snapshot_id()
    full_samples = [sample for sample in samples if sample.execution_condition == ExecutionCondition.FULL.value]
    baseline_samples = [sample for sample in samples if sample.execution_condition != ExecutionCondition.FULL.value]
    full_path = f"samples/full-{snapshot_id}.jsonl"
    baseline_path = f"samples/matched-baseline-{snapshot_id}.jsonl"
    write_jsonl(output / full_path, full_samples)
    write_jsonl(output / baseline_path, baseline_samples)
    environment = {
        "config_hash": configuration_hash,
        "provider": provider,
        "model": model,
        "temperature": "provider_default" if temperature is None else str(temperature),
        "judge_provider": judge_provider,
        "judge_model": judge_model,
        "formal_sampling": str(formal_sampling).lower(),
        "dataset_content_hash": dataset.content_hash,
        "preflight_per_condition": str(dataset.preflight_per_condition),
        "timeout_seconds": "unspecified" if timeout_seconds is None else str(timeout_seconds),
        "retry_count": "unspecified" if retry_count is None else str(retry_count),
        "workflow_version": _HARNESS_WORKFLOW_VERSION,
    }
    full_snapshot = _snapshot(dataset, full_samples, f"{snapshot_id}-full", full_path, repeat, environment, "full", _samples_content_summary(output / full_path))
    baseline_snapshot = _snapshot(dataset, baseline_samples, f"{snapshot_id}-matched-baseline", baseline_path, repeat, environment, "matched_baseline", _samples_content_summary(output / baseline_path))
    (output / f"{full_snapshot.snapshot_id}.json").write_text(json.dumps(full_snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / f"{baseline_snapshot.snapshot_id}.json").write_text(json.dumps(baseline_snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if len(selected) == len(dataset.instances) and repeat == dataset.formal_repeat:
        summary = summarize_harness_business(dataset, samples, require_formal=formal_sampling)
        write_harness_business_report(output, summary)
    else:
        # 局部运行只用于开发排障，不生成可被误认作业务效果的正式汇总。
        (output / "diagnostic-summary.json").write_text(
            json.dumps({"formal_sampling": False, "instance_ids": [item.instance_id for item in selected], "sample_count": len(samples)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    (output / "business-config.json").write_text(
        json.dumps({**environment, "dataset": dataset.dataset_id, "repeat": repeat, "instance_count": len(selected)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return full_snapshot, baseline_snapshot


async def _run_preflights(
    dataset: HarnessDataset,
    selected: Sequence[HarnessTaskInstance],
    executor: HarnessTaskExecutor,
) -> list[dict[str, object]]:
    """每种实际执行条件只运行一次不计入正式样本的链路检查。"""
    first_by_condition: dict[ExecutionCondition, HarnessTaskInstance] = {ExecutionCondition.FULL: selected[0]}
    for instance in selected:
        first_by_condition.setdefault(dataset.baseline_for(instance.family), instance)
    results: list[dict[str, object]] = []
    for condition, instance in first_by_condition.items():
        result = await executor.execute(instance, condition, attempt=0, preflight=True)
        results.append({
            "instance_id": instance.instance_id,
            "execution_condition": condition.value,
            "runtime_reached": result.run_id is not None,
            "trace_available": result.trace_available,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
    return results


async def _run_sample(
    dataset: HarnessDataset,
    instance: HarnessTaskInstance,
    condition: ExecutionCondition,
    attempt: int,
    executor: HarnessTaskExecutor,
    judge: JudgePort,
    *,
    provider: str,
    model: str,
    temperature: float | None,
    judge_provider: str,
    judge_model: str,
    formal_sampling: bool,
    commit: str,
    configuration_hash: str,
) -> BenchmarkSample:
    """执行确定性链路，并只为合格候选调用一次 Judge。"""
    started = time.perf_counter()
    try:
        execution = await executor.execute(instance, condition, attempt=attempt, preflight=False)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        execution = HarnessExecutionResult("", False, frozenset(), None, False, (time.perf_counter() - started) * 1000, 0, 0, type(error).__name__, "runtime_failure")
    required_checks = _required_checks(instance, condition)
    missing_checks = required_checks - set(execution.observed_checks)
    deterministic = execution.deterministic_passed and not missing_checks
    spec = JudgeSpec.from_harness_instance(instance)
    judge_verdict: str | None = None
    judge_criteria: dict[str, str] | None = None
    judge_reason = ""
    candidate_redacted, candidate_redaction_applied = redact_review_text(execution.candidate)
    judge_reason_redacted = ""
    judge_reason_redaction_applied = False
    failure_attribution = execution.failure_attribution
    if deterministic:
        if not execution.candidate:
            deterministic = False
            failure_attribution = "missing_final_delivery"
        else:
            try:
                verdict = parse_verdict(await judge.judge(render_prompt(spec, execution.candidate)), spec)
                judge_verdict = verdict.verdict
                judge_criteria = dict(verdict.criteria)
                judge_reason = verdict.reason
                judge_reason_redacted, judge_reason_redaction_applied = redact_review_text(judge_reason)
                if verdict.verdict == "fail":
                    failure_attribution = "judge_quality_failure"
            except (JudgeProtocolError, TimeoutError) as error:
                judge_verdict = "error"
                error_reason = str(error) or type(error).__name__
                judge_reason_redacted, judge_reason_redaction_applied = redact_review_text(error_reason)
                failure_attribution = "judge_error"
    elif failure_attribution is None:
        failure_attribution = "assertion_failure"
    task_success = deterministic and judge_verdict == "pass"
    fixture_fingerprint = hashlib.sha256(
        f"{dataset.content_hash}:{instance.instance_id}:{_HARNESS_WORKFLOW_VERSION}".encode("utf-8")
    ).hexdigest()[:16]
    evidence_summary: dict[str, object] = {
        "deterministic_required": sorted(required_checks),
        "deterministic_observed": sorted(required_checks & set(execution.observed_checks)),
        "deterministic_missing": sorted(missing_checks),
    }
    if execution.evidence_summary is not None:
        evidence_summary["runtime_evidence"] = dict(execution.evidence_summary)
    return BenchmarkSample(
        dataset=dataset.dataset_id,
        case_id=instance.instance_id,
        attempt=attempt,
        is_warmup=False,
        git_commit=commit,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        config_hash=configuration_hash,
        eval_schema_version="1.0",
        passed=task_success,
        failure_kind=execution.failure_kind if execution.failure_kind is not None else (None if task_success else failure_attribution),
        assertions_passed=len(required_checks) - len(missing_checks) if execution.deterministic_passed else 0,
        assertions_total=len(required_checks),
        trace_available=execution.trace_available,
        wall_duration_ms=execution.wall_duration_ms,
        run_id=execution.run_id,
        fixture_fingerprint=fixture_fingerprint,
        execution_mode="ext",
        deterministic_passed=deterministic,
        failure_attribution=failure_attribution,
        llm_call_count=execution.llm_call_count,
        tool_call_count=execution.tool_call_count,
        judge_spec_version=spec.version,
        judge_verdict=judge_verdict,
        judge_criteria=judge_criteria,
        provider=provider,
        model=model,
        temperature=temperature,
        judge_provider=judge_provider,
        judge_model=judge_model,
        judge_prompt_hash=prompt_hash(spec),
        candidate_delivery_redacted=candidate_redacted or None,
        judge_reason_redacted=judge_reason_redacted or None,
        review_redaction_applied=candidate_redaction_applied or judge_reason_redaction_applied,
        dataset_version=dataset.version,
        workflow_version=_HARNESS_WORKFLOW_VERSION,
        formal_sampling=formal_sampling,
        task_family=instance.family.value,
        instance_id=instance.instance_id,
        difficulty=instance.difficulty.value,
        execution_condition=condition.value,
        baseline_for=dataset.baseline_for(instance.family).value,
        capability_tags=instance.capability_tags,
        task_success=task_success,
        judge_criterion_dimensions=instance.criterion_dimensions,
        run_statistics={"llm_call_count": execution.llm_call_count, "tool_call_count": execution.tool_call_count},
        evidence_summary=evidence_summary,
    )


def _snapshot(
    dataset: HarnessDataset,
    samples: Sequence[BenchmarkSample],
    snapshot_id: str,
    samples_path: str,
    repeat: int,
    environment: dict[str, str],
    condition: str,
    content_summary: dict[str, object],
) -> BenchmarkSnapshot:
    """每种候选条件单独生成快照，避免按 Case 混合 Full/Baseline。"""
    return build_snapshot(
        snapshot_id=snapshot_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        git_commit=git_short_commit(),
        dataset=dataset.dataset_id,
        environment={**environment, "execution_profile": condition},
        warmup=0,
        repeat=repeat,
        samples=samples,
        samples_path=samples_path,
        scenario_id=condition,
        samples_content_summary=content_summary,
    )


def _samples_content_summary(path: Path) -> dict[str, object]:
    """将原始 JSONL 的行数、字节数和内容哈希绑定进对应快照。"""
    content = path.read_bytes()
    return {
        "line_count": len(content.splitlines()),
        "byte_count": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _required_checks(instance: HarnessTaskInstance, condition: ExecutionCondition) -> set[str]:
    """Baseline 只移除被消融能力的机制断言，保留业务副作用与安全约束。"""
    checks = set(instance.deterministic_checks)
    if condition is ExecutionCondition.FULL:
        return checks
    removable_fragments: dict[ExecutionCondition, tuple[str, ...]] = {
        ExecutionCondition.SINGLE_PASS_RESEARCH: ("source", "research"),
        ExecutionCondition.SINGLE_AGENT_WORKSPACE: ("workspace_read", "diagnosis_recorded"),
        ExecutionCondition.DIRECT_TOOL_WITHOUT_RESUME: (),
        ExecutionCondition.RECENT_WINDOW_ONLY: ("context", "history", "raw_tail", "session_"),
        ExecutionCondition.SINGLE_AGENT_NO_DELEGATION: ("delegation", "child", "parent_consumed", "chain_", "cross_chain", "followup"),
        ExecutionCondition.PRIMARY_CAPABILITY_REMOVED: ("context", "history", "delegation", "child", "parent_consumed", "approval_observed"),
    }
    fragments = removable_fragments[condition]
    retained = {check for check in checks if not any(fragment in check for fragment in fragments)}
    return retained or {"runtime_completed"}


def _select_instances(dataset: HarnessDataset, instance_ids: Sequence[str] | None) -> tuple[HarnessTaskInstance, ...]:
    """开发诊断允许选择实例；未知或重复 ID 明确拒绝。"""
    if instance_ids is None:
        return dataset.instances
    requested = tuple(instance_ids)
    if not requested or len(requested) != len(set(requested)):
        raise HarnessBusinessRunError("诊断实例 ID 不得为空或重复")
    by_id = {item.instance_id: item for item in dataset.instances}
    if any(item not in by_id for item in requested):
        raise HarnessBusinessRunError("诊断请求包含未知实例 ID")
    return tuple(by_id[item] for item in requested)
