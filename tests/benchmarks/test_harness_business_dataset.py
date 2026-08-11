"""PR8 Agent Harness 业务 Dataset 契约测试。"""

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from benchmarks.business_judge import JudgeSpec, parse_verdict, render_prompt
from benchmarks.eval_baseline_models import BenchmarkSample
from benchmarks.harness_business_dataset import Difficulty, HarnessDatasetError, QualityDimension, TaskFamily, load_harness_dataset


def test_harness_dataset_has_confirmed_shape() -> None:
    """正式 Dataset 必须精确满足六族、六十实例和 12/30/18 分布。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))

    assert len(dataset.families) == 6
    assert len(dataset.instances) == 60
    assert Counter(item.family for item in dataset.instances) == {family: 10 for family in TaskFamily}
    assert Counter(item.difficulty for item in dataset.instances) == {
        Difficulty.EASY: 12,
        Difficulty.MEDIUM: 30,
        Difficulty.HARD: 18,
    }
    assert dataset.formal_repeat == 3
    assert dataset.preflight_per_condition == 1
    assert len(dataset.content_hash) == 64


def test_hard_instances_have_multiple_complexity_factors() -> None:
    """Hard 实例必须组合至少两类复杂因素，而非只增加文字长度。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))

    hard = [item for item in dataset.instances if item.difficulty is Difficulty.HARD]
    assert len(hard) == 18
    assert all(len(set(item.complexity_factors)) >= 2 for item in hard)


def test_dataset_rejects_wrong_family_distribution(tmp_path: Path) -> None:
    """单个任务族数量或难度分布错误时不得形成正式 Dataset。"""
    source = Path("benchmarks/datasets/agent_harness_business_v1")
    target = tmp_path / "agent_harness_business_v1"
    shutil.copytree(source, target)
    path = target / "instances/evidence_research.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[0]["difficulty"] = "medium"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(HarnessDatasetError, match="难度必须为 2/5/3"):
        load_harness_dataset(tmp_path)


def test_dataset_rejects_hard_instance_with_one_factor(tmp_path: Path) -> None:
    """Hard 实例缺少第二种复杂因素时必须立即拒绝。"""
    source = Path("benchmarks/datasets/agent_harness_business_v1")
    target = tmp_path / "agent_harness_business_v1"
    shutil.copytree(source, target)
    path = target / "instances/mixed_complex.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[-1]["complexity_factors"] = ["long_context"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(HarnessDatasetError, match="Hard 实例至少需要两种复杂因素"):
        load_harness_dataset(tmp_path)


def test_harness_judge_spec_uses_atomic_dimensions() -> None:
    """新 Judge Prompt 必须携带原子判据的维度与必需属性。"""
    instance = load_harness_dataset(Path("benchmarks/datasets")).instances[0]
    spec = JudgeSpec.from_harness_instance(instance)
    payload = json.loads(render_prompt(spec, "候选交付").split("\n", 1)[1])

    assert spec.version == "3"
    assert set(spec.required_criteria) == {item.criterion_id for item in instance.criteria if item.required}
    assert {item["dimension"] for item in payload["criteria"]}.issubset({item.value for item in QualityDimension})
    assert all(set(item) == {"id", "description", "dimension", "required"} for item in payload["criteria"])


def test_optional_judge_failure_does_not_fail_required_verdict() -> None:
    """可选质量观察失败不能覆盖全部必需判据已通过的任务结论。"""
    instance = load_harness_dataset(Path("benchmarks/datasets")).instances[0]
    spec = JudgeSpec.from_harness_instance(instance)
    criteria = {criterion_id: "pass" for criterion_id in spec.criteria}
    raw = json.dumps({"verdict": "pass", "criteria": criteria, "reason": "全部必需判据通过"}, ensure_ascii=False)

    assert parse_verdict(raw, spec).verdict == "pass"


def test_business_sample_round_trip_keeps_harness_fields() -> None:
    """新业务字段必须可序列化，旧样本缺失字段时仍保持可选。"""
    sample = BenchmarkSample(
        dataset="agent_harness_business_v1",
        case_id="evidence-01-vendor-decision",
        attempt=0,
        is_warmup=False,
        git_commit="abc123",
        python_version="3.13",
        platform="test",
        config_hash="hash",
        eval_schema_version="1.0",
        passed=True,
        failure_kind=None,
        assertions_passed=2,
        assertions_total=2,
        trace_available=True,
        wall_duration_ms=12.0,
        run_id="run-1",
        task_family="evidence_research",
        instance_id="evidence-01-vendor-decision",
        difficulty="easy",
        execution_condition="full",
        baseline_for="single_pass_research",
        capability_tags=("research", "grounding"),
        task_success=True,
        judge_criterion_dimensions={"select_a": "groundedness"},
    )

    restored = BenchmarkSample.from_dict(sample.to_dict())
    assert restored.capability_tags == ("research", "grounding")
    assert restored.task_success is True
    assert restored.judge_criterion_dimensions == {"select_a": "groundedness"}
