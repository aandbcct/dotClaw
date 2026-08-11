"""PR8 Agent Harness 业务效果的配对统计与正式资格校验。"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .eval_baseline_models import BenchmarkSample
from .eval_baseline_stats import percentile
from .business_judge import JudgeSpec, prompt_hash
from .harness_business_dataset import HarnessDataset, QualityDimension


class HarnessBusinessReportError(ValueError):
    """业务效果样本缺少完整配对或追溯资格。"""


def summarize_harness_business(
    dataset: HarnessDataset,
    samples: Sequence[BenchmarkSample],
    *,
    require_formal: bool = True,
) -> Mapping[str, object]:
    """汇总 Full 与匹配 Baseline，不把 preflight 或诊断样本计入分母。"""
    formal = [sample for sample in samples if not sample.is_warmup]
    _validate_samples(dataset, formal, require_formal=require_formal)
    full = [sample for sample in formal if sample.execution_condition == "full"]
    baselines = [sample for sample in formal if sample.execution_condition != "full"]
    paired = _paired_instance_rates(dataset, full, baselines)
    full_rate = _success_rate(full)
    baseline_rate = _success_rate(baselines)
    result: dict[str, object] = {
        "dataset": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_content_hash": dataset.content_hash,
        "qualification": "formal" if all(sample.formal_sampling is True for sample in formal) else "development",
        "formal_sampling": all(sample.formal_sampling is True for sample in formal),
        "git_commit": formal[0].git_commit,
        "config_hash": formal[0].config_hash,
        "candidate_condition": {
            "provider": formal[0].provider,
            "model": formal[0].model,
            "temperature": formal[0].temperature,
        },
        "judge_condition": {"provider": formal[0].judge_provider, "model": formal[0].judge_model},
        "instance_count": len(dataset.instances),
        "formal_repeat": dataset.formal_repeat,
        "sample_count": len(formal),
        "full": _group_summary(full),
        "matched_baseline": _group_summary(baselines),
        "harness_value": {
            "full_success_rate": full_rate,
            "baseline_success_rate": baseline_rate,
            "lift_percentage_points": (full_rate - baseline_rate) * 100.0,
            "cluster_bootstrap_95_percentage_points": _cluster_bootstrap_interval(paired),
        },
        "delivery_quality": {
            **_quality_summary(formal),
            "by_execution_condition": {
                condition: _quality_summary([sample for sample in formal if sample.execution_condition == condition])
                for condition in sorted({sample.execution_condition or "" for sample in formal})
            },
        },
        "by_family": _partition_summary(formal, "task_family"),
        "by_difficulty": _partition_summary(formal, "difficulty"),
        "by_execution_condition": _partition_summary(formal, "execution_condition"),
    }
    return result


def write_harness_business_report(output: Path, summary: Mapping[str, object]) -> tuple[Path, Path]:
    """写出机器可读 JSON 与审阅用 Markdown；已存在时拒绝覆盖。"""
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "harness-business-summary.json"
    markdown_path = output / "harness-business-summary.md"
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError("Agent Harness 业务效果报告已存在，拒绝覆盖")
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    json_path.write_text(rendered + "\n", encoding="utf-8")
    qualification = str(summary.get("qualification", "unknown"))
    warning = "该报告具备正式采样资格。" if qualification == "formal" else "该报告仅为开发验证，不得作为业务效果或简历证据。"
    markdown_path.write_text(
        "# PR8 Agent Harness 业务效果报告\n\n"
        f"> {warning} 结果只描述固定 Dataset、模型、配置和匹配 Baseline；不代表线上用户成功率。\n\n"
        "```json\n" + rendered + "\n```\n",
        encoding="utf-8",
    )
    return json_path, markdown_path


def _validate_samples(dataset: HarnessDataset, samples: Sequence[BenchmarkSample], *, require_formal: bool) -> None:
    """验证字段、模型条件和 60×3×2 完整配对矩阵。"""
    if len(samples) != len(dataset.instances) * dataset.formal_repeat * 2:
        raise HarnessBusinessReportError("正式样本必须精确覆盖 Full 180 条与匹配 Baseline 180 条")
    instance_by_id = {item.instance_id: item for item in dataset.instances}
    required_fields = (
        "task_family",
        "instance_id",
        "difficulty",
        "execution_condition",
        "baseline_for",
        "task_success",
        "deterministic_passed",
    )
    commits = {sample.git_commit for sample in samples}
    configs = {sample.config_hash for sample in samples}
    candidate_models = {(sample.provider, sample.model, sample.temperature) for sample in samples}
    judge_models = {(sample.judge_provider, sample.judge_model) for sample in samples}
    formal_flags = {sample.formal_sampling for sample in samples}
    if len(commits) != 1 or len(configs) != 1 or len(candidate_models) != 1 or len(judge_models) != 1:
        raise HarnessBusinessReportError("正式样本混入不同提交、配置、候选模型或 Judge 条件")
    if len(formal_flags) != 1:
        raise HarnessBusinessReportError("样本混入不同 formal_sampling 资格")
    seen: set[tuple[str, int, str]] = set()
    for sample in samples:
        if any(getattr(sample, field) is None for field in required_fields):
            raise HarnessBusinessReportError("新业务样本缺少任务族、实例、难度、执行条件或任务成功字段")
        if require_formal and sample.formal_sampling is not True:
            raise HarnessBusinessReportError("正式报告拒绝未标记 formal_sampling 的样本")
        if sample.dataset != dataset.dataset_id or sample.dataset_version != dataset.version:
            raise HarnessBusinessReportError("样本 Dataset 标识或版本不一致")
        instance = instance_by_id.get(sample.instance_id or "")
        if instance is None:
            raise HarnessBusinessReportError("样本引用未知实例")
        baseline = dataset.baseline_for(instance.family).value
        if sample.task_family != instance.family.value or sample.difficulty != instance.difficulty.value:
            raise HarnessBusinessReportError("样本任务族或难度与 Dataset 不一致")
        if sample.baseline_for != baseline or sample.execution_condition not in {"full", baseline}:
            raise HarnessBusinessReportError("样本执行条件不是实例对应的 Full 或匹配 Baseline")
        expected_fingerprint = hashlib.sha256(f"{dataset.content_hash}:{instance.instance_id}".encode("utf-8")).hexdigest()[:16]
        judge_spec = JudgeSpec.from_harness_instance(instance)
        if sample.fixture_fingerprint != expected_fingerprint:
            raise HarnessBusinessReportError("样本 Dataset 内容指纹与当前版本不一致")
        if sample.judge_spec_version != judge_spec.version or sample.judge_prompt_hash != prompt_hash(judge_spec):
            raise HarnessBusinessReportError("样本 Judge 规范或 Prompt 指纹与当前版本不一致")
        if sample.capability_tags != instance.capability_tags or sample.judge_criterion_dimensions != instance.criterion_dimensions:
            raise HarnessBusinessReportError("样本能力标签或 Judge 质量维度与 Dataset 不一致")
        derived_success = sample.deterministic_passed is True and sample.judge_verdict == "pass"
        if sample.task_success is not derived_success or sample.passed is not derived_success:
            raise HarnessBusinessReportError("样本 Task Success 与确定性门禁/Judge 结果不一致")
        if sample.deterministic_passed is False and sample.judge_verdict is not None:
            raise HarnessBusinessReportError("确定性门禁失败的样本不得进入 Judge")
        if not 0 <= sample.attempt < dataset.formal_repeat:
            raise HarnessBusinessReportError("样本 attempt 超出正式重复范围")
        key = (instance.instance_id, sample.attempt, sample.execution_condition or "")
        if key in seen:
            raise HarnessBusinessReportError("正式样本存在重复配对键")
        seen.add(key)
    expected = {
        (instance.instance_id, attempt, condition)
        for instance in dataset.instances
        for attempt in range(dataset.formal_repeat)
        for condition in ("full", dataset.baseline_for(instance.family).value)
    }
    if seen != expected:
        raise HarnessBusinessReportError("正式样本缺少 Full/Baseline 配对键")


def _paired_instance_rates(
    dataset: HarnessDataset,
    full: Sequence[BenchmarkSample],
    baselines: Sequence[BenchmarkSample],
) -> Mapping[str, tuple[float, float]]:
    """按实例聚合三次重复，避免把 180 次运行当作独立任务。"""
    result: dict[str, tuple[float, float]] = {}
    for instance in dataset.instances:
        full_items = [sample for sample in full if sample.instance_id == instance.instance_id]
        baseline_items = [sample for sample in baselines if sample.instance_id == instance.instance_id]
        result[instance.instance_id] = (_success_rate(full_items), _success_rate(baseline_items))
    return result


def _cluster_bootstrap_interval(paired: Mapping[str, tuple[float, float]]) -> tuple[float, float]:
    """按实例有放回抽样，返回配对提升的稳定 95% 区间。"""
    values = list(paired.values())
    generator = random.Random(20260811)
    draws: list[float] = []
    for _ in range(5000):
        selected = [values[generator.randrange(len(values))] for _ in values]
        draws.append(sum(full - baseline for full, baseline in selected) / len(selected) * 100.0)
    return percentile(draws, 2.5), percentile(draws, 97.5)


def _quality_summary(samples: Sequence[BenchmarkSample]) -> Mapping[str, object]:
    """按原子判据维度统计质量；未进入 Judge 的样本不伪装为质量观察。"""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    unsupported_claims: Counter[str] = Counter()
    entered_judge = 0
    for sample in samples:
        if sample.judge_criteria is None or sample.judge_criterion_dimensions is None:
            continue
        entered_judge += 1
        if set(sample.judge_criteria) != set(sample.judge_criterion_dimensions):
            raise HarnessBusinessReportError("Judge 判据结果与质量维度映射不一致")
        for criterion_id, verdict in sample.judge_criteria.items():
            dimension = sample.judge_criterion_dimensions[criterion_id]
            if dimension not in {item.value for item in QualityDimension} or verdict not in {"pass", "fail"}:
                raise HarnessBusinessReportError("Judge 判据维度或 verdict 非法")
            counts[dimension][verdict] += 1
            if criterion_id == "unsupported_claim_absent":
                unsupported_claims[verdict] += 1
    dimensions: dict[str, object] = {}
    for dimension in QualityDimension:
        passed = counts[dimension.value]["pass"]
        failed = counts[dimension.value]["fail"]
        total = passed + failed
        dimensions[dimension.value] = {
            "observations": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": None if total == 0 else passed / total,
        }
    constraints = counts[QualityDimension.CONSTRAINT_COMPLIANCE.value]
    unsupported_total = unsupported_claims["pass"] + unsupported_claims["fail"]
    constraint_total = constraints["pass"] + constraints["fail"]
    return {
        "entered_judge": entered_judge,
        "dimensions": dimensions,
        "fact_overreach_rate": None if unsupported_total == 0 else unsupported_claims["fail"] / unsupported_total,
        "constraint_violation_rate": None if constraint_total == 0 else constraints["fail"] / constraint_total,
    }


def _partition_summary(samples: Sequence[BenchmarkSample], field: str) -> Mapping[str, object]:
    """按单一维度分区，同时保留执行条件，避免混合 Full 与 Baseline。"""
    values = sorted({str(getattr(sample, field)) for sample in samples})
    result: dict[str, object] = {}
    for value in values:
        subset = [sample for sample in samples if str(getattr(sample, field)) == value]
        result[value] = {
            condition: _group_summary([sample for sample in subset if sample.execution_condition == condition])
            for condition in sorted({sample.execution_condition or "" for sample in subset})
        }
    return result


def _group_summary(samples: Sequence[BenchmarkSample]) -> Mapping[str, object]:
    """形成带分母的任务成功、时延、调用次数和失败归因。"""
    if not samples:
        return {"sample_count": 0}
    successes = sum(sample.task_success is True for sample in samples)
    return {
        "sample_count": len(samples),
        "task_successes": successes,
        "task_success_rate": successes / len(samples),
        "p50_ms": percentile([sample.wall_duration_ms for sample in samples], 50),
        "p95_ms": percentile([sample.wall_duration_ms for sample in samples], 95),
        "llm_call_count": sum(sample.llm_call_count or 0 for sample in samples),
        "tool_call_count": sum(sample.tool_call_count or 0 for sample in samples),
        "failure_attribution": dict(sorted(Counter(sample.failure_attribution for sample in samples if sample.failure_attribution).items())),
    }


def _success_rate(samples: Sequence[BenchmarkSample]) -> float:
    """计算显式任务成功率；调用方保证分母非零。"""
    if not samples:
        raise HarnessBusinessReportError("任务成功率分母不得为零")
    return sum(sample.task_success is True for sample in samples) / len(samples)
