"""PR8 Agent Harness 业务效果配对报告测试。"""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks.eval_baseline_models import BenchmarkSample
from benchmarks.business_judge import JudgeSpec, prompt_hash
from benchmarks.harness_business_dataset import HarnessDataset, HarnessTaskInstance, load_harness_dataset
from benchmarks.harness_business_report import HarnessBusinessReportError, summarize_harness_business


def _sample(instance: HarnessTaskInstance, condition: str, attempt: int, success: bool) -> BenchmarkSample:
    """构造具备完整追溯字段的正式样本。"""
    criteria = {item.criterion_id: "pass" if success else "fail" for item in instance.criteria}
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    spec = JudgeSpec.from_harness_instance(instance)
    return BenchmarkSample(
        dataset="agent_harness_business_v1",
        case_id=instance.instance_id,
        attempt=attempt,
        is_warmup=False,
        git_commit="abc123",
        python_version="3.13",
        platform="test",
        config_hash="config",
        eval_schema_version="1.0",
        passed=success,
        failure_kind=None if success else "judge_quality",
        assertions_passed=2,
        assertions_total=2,
        trace_available=True,
        wall_duration_ms=10.0 + attempt,
        run_id=f"{instance.instance_id}-{condition}-{attempt}",
        fixture_fingerprint=hashlib.sha256(f"{dataset.content_hash}:{instance.instance_id}".encode("utf-8")).hexdigest()[:16],
        execution_mode="ext",
        deterministic_passed=True,
        failure_attribution=None if success else "judge_quality_failure",
        llm_call_count=2,
        tool_call_count=1,
        judge_verdict="pass" if success else "fail",
        judge_criteria=criteria,
        judge_spec_version=spec.version,
        judge_prompt_hash=prompt_hash(spec),
        provider="provider",
        model="model",
        temperature=0.0,
        judge_provider="provider",
        judge_model="judge",
        dataset_version="1",
        formal_sampling=True,
        task_family=instance.family.value,
        instance_id=instance.instance_id,
        difficulty=instance.difficulty.value,
        execution_condition=condition,
        baseline_for="",
        capability_tags=instance.capability_tags,
        task_success=success,
        judge_criterion_dimensions=instance.criterion_dimensions,
    )


def _complete_samples(dataset: HarnessDataset) -> list[BenchmarkSample]:
    """Full 全成功、匹配 Baseline 全失败，便于核对配对提升。"""
    samples: list[BenchmarkSample] = []
    for instance in dataset.instances:
        baseline = dataset.baseline_for(instance.family).value
        for attempt in range(dataset.formal_repeat):
            samples.append(replace(_sample(instance, "full", attempt, True), baseline_for=baseline))
            samples.append(replace(_sample(instance, baseline, attempt, False), baseline_for=baseline))
    return samples


def test_summary_reports_full_matched_baseline_and_quality() -> None:
    """报告必须输出 180/180 配对结果、百分点提升与质量违反率。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    summary = summarize_harness_business(dataset, _complete_samples(dataset))

    assert summary["sample_count"] == 360
    assert summary["qualification"] == "formal"
    assert summary["formal_sampling"] is True
    assert summary["full"]["sample_count"] == 180
    assert summary["matched_baseline"]["sample_count"] == 180
    assert summary["harness_value"]["lift_percentage_points"] == 100.0
    assert summary["delivery_quality"]["entered_judge"] == 360
    assert summary["delivery_quality"]["constraint_violation_rate"] == 0.5
    assert summary["delivery_quality"]["by_execution_condition"]["full"]["constraint_violation_rate"] == 0.0
    assert set(summary["by_family"]) == {item.family.value for item in dataset.families}
    assert set(summary["by_difficulty"]) == {"easy", "medium", "hard"}


def test_summary_rejects_missing_pair() -> None:
    """缺少任一实例 attempt 的 Full 或 Baseline 时不得计算 Harness Value。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    samples = _complete_samples(dataset)

    with pytest.raises(HarnessBusinessReportError, match="精确覆盖"):
        summarize_harness_business(dataset, samples[:-1])


def test_summary_rejects_mixed_model_conditions() -> None:
    """同一配对矩阵混入不同候选模型时必须拒绝。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    samples = _complete_samples(dataset)
    samples[-1] = replace(samples[-1], model="other-model")

    with pytest.raises(HarnessBusinessReportError, match="混入不同提交、配置、候选模型"):
        summarize_harness_business(dataset, samples)


def test_development_summary_can_skip_formal_flag_but_not_pairing() -> None:
    """开发期可关闭正式标记门禁，但仍必须保持完整配对口径。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    samples = [replace(item, formal_sampling=False) for item in _complete_samples(dataset)]

    summary = summarize_harness_business(dataset, samples, require_formal=False)
    assert summary["sample_count"] == 360
    assert summary["qualification"] == "development"


def test_summary_rejects_task_success_inconsistent_with_judge() -> None:
    """Task Success 不能脱离确定性门禁和 Judge 结果被直接写成成功。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    samples = _complete_samples(dataset)
    samples[-1] = replace(samples[-1], passed=True, task_success=True)

    with pytest.raises(HarnessBusinessReportError, match="Task Success"):
        summarize_harness_business(dataset, samples)
