"""PR8 本地业务报告与 PR1 至 PR7 证据资格收口。"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from .eval_baseline_models import BenchmarkSample, BenchmarkSnapshot
from .eval_baseline_stats import percentile


class BusinessReportError(ValueError):
    """报告输入缺少可追溯正式证据时抛出。"""


def summarize_business_samples(samples: Sequence[BenchmarkSample], mode: str) -> Mapping[str, object]:
    """按 Fixture/EXT 严格分区汇总，不混合通过率或失败归因。"""
    formal = [item for item in samples if not item.is_warmup and item.execution_mode == mode]
    if not formal:
        raise BusinessReportError(f"没有 {mode} 正式样本")
    if mode not in {"fixture", "ext"}:
        raise BusinessReportError(f"不支持的业务执行模式：{mode}")
    if any(item.task_category is None or item.task_kind is None or item.deterministic_passed is None for item in formal):
        raise BusinessReportError(f"{mode} 样本缺少业务分类或确定性结果")
    categories = {item.task_category for item in formal if item.task_category}
    failures: dict[str, int] = {}
    for item in formal:
        if item.failure_attribution:
            failures[item.failure_attribution] = failures.get(item.failure_attribution, 0) + 1
    deterministic = sum(item.deterministic_passed is True for item in formal)
    by_task = {task_id: _rate_summary([item for item in formal if item.case_id == task_id]) for task_id in sorted({item.case_id for item in formal})}
    by_category = {category: _rate_summary([item for item in formal if item.task_category == category]) for category in sorted(categories)}
    result: dict[str, object] = {"sample_count": len(formal), "task_count": len({item.case_id for item in formal}), "category_count": len(categories), "deterministic_passed": deterministic, "completion_wilson_95": _wilson(deterministic, len(formal)), "p50_ms": percentile([item.wall_duration_ms for item in formal], 50), "p95_ms": percentile([item.wall_duration_ms for item in formal], 95), "llm_call_count": sum(item.llm_call_count or 0 for item in formal), "tool_call_count": sum(item.tool_call_count or 0 for item in formal), "failure_attribution": failures, "by_task": by_task, "by_category": by_category}
    if mode == "ext":
        judged = [item for item in formal if item.judge_verdict is not None]
        criterion_failures: Counter[str] = Counter()
        for item in judged:
            for criterion_id, verdict in (item.judge_criteria or {}).items():
                if verdict == "fail":
                    criterion_failures[criterion_id] += 1
        quality_passed = sum(item.judge_verdict == "pass" for item in judged)
        result.update({"entered_judge": len(judged), "quality_passed": quality_passed, "quality_wilson_95": _wilson(quality_passed, len(judged)) if judged else None, "judge_errors": failures.get("judge_error", 0), "judge_criterion_failures": dict(sorted(criterion_failures.items()))})
    return result


def _wilson(passed: int, total: int) -> tuple[float, float]:
    """计算 Wilson 95% 区间；调用方保证总样本数大于零。"""
    z = 1.959963984540054
    rate = passed / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return centre - margin, centre + margin


def _rate_summary(samples: Sequence[BenchmarkSample]) -> Mapping[str, object]:
    """按单任务或单类别形成带分母的完成率和时延汇总。"""
    passed = sum(item.deterministic_passed is True for item in samples)
    failure_attribution = Counter(item.failure_attribution for item in samples if item.failure_attribution)
    return {"sample_count": len(samples), "passed": passed, "failed": len(samples) - passed, "wilson_95": _wilson(passed, len(samples)), "p50_ms": percentile([item.wall_duration_ms for item in samples], 50), "p95_ms": percentile([item.wall_duration_ms for item in samples], 95), "failure_attribution": dict(sorted(failure_attribution.items()))}


def write_business_reports(output: Path, fixture: Mapping[str, object], ext: Mapping[str, object] | None = None) -> None:
    """写出正式分区报告；调用方必须先完成样本资格校验。"""
    lines = ["# PR8 业务质量基线报告", "", "## Fixture", "", json.dumps(fixture, ensure_ascii=False, indent=2)]
    if ext is not None:
        lines.extend(["", "## EXT", "", json.dumps(ext, ensure_ascii=False, indent=2)])
    (output / "business-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_final_evidence(manifest_path: Path, fixture_snapshot: BenchmarkSnapshot, ext_snapshot: BenchmarkSnapshot) -> Mapping[str, object]:
    """最终 README/简历数字前要求 PR1-PR7 清单、两类 PR8 快照及正式采样资格齐全。"""
    if not manifest_path.is_file():
        raise BusinessReportError("缺少 PR1 至 PR7 证据清单，拒绝生成跨 PR 结论")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or len(entries) != 7:
        raise BusinessReportError("PR1 至 PR7 证据清单不完整")
    _validate_manifest_entries(entries)
    _validate_pr8_snapshot(fixture_snapshot, "fixture", expected_tasks=10, expected_samples=300)
    _validate_pr8_snapshot(ext_snapshot, "ext", expected_tasks=6, expected_samples=180)
    return manifest


def _validate_manifest_entries(entries: Sequence[object]) -> None:
    """验证 PR1 至 PR7 证据清单的每条资格，拒绝仅按条目数凑齐。"""
    suites: set[str] = set()
    required = {"snapshot", "suite", "git_commit", "samples_path", "formal_sample_count", "fixture_fingerprints", "environment"}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or required - set(entry):
            raise BusinessReportError(f"PR1 至 PR7 证据条目 {index} 缺少资格字段")
        suite = entry["suite"]
        commit = entry["git_commit"]
        count = entry["formal_sample_count"]
        fingerprints = entry["fixture_fingerprints"]
        environment = entry["environment"]
        if not isinstance(suite, str) or not suite or suite in suites:
            raise BusinessReportError("PR1 至 PR7 证据任务集必须为七个唯一非空值")
        if not isinstance(commit, str) or not commit or commit == "unknown" or not isinstance(count, int) or count <= 0:
            raise BusinessReportError(f"PR1 至 PR7 证据条目 {index} 缺少固定提交或正式样本")
        if not isinstance(fingerprints, dict) or not fingerprints or not all(isinstance(value, str) and value for value in fingerprints.values()):
            raise BusinessReportError(f"PR1 至 PR7 证据条目 {index} 缺少 Fixture 指纹")
        if not isinstance(environment, dict) or not {"python_version", "platform", "config_hash", "eval_schema_version"}.issubset(environment):
            raise BusinessReportError(f"PR1 至 PR7 证据条目 {index} 缺少环境追溯字段")
        suites.add(suite)


def _validate_pr8_snapshot(snapshot: BenchmarkSnapshot, mode: str, *, expected_tasks: int, expected_samples: int) -> None:
    """验证单个 PR8 快照的固定口径，避免快照摘要与正式任务集脱节。"""
    if snapshot.dataset != "runtime_core_v2" or snapshot.repeat != 30 or snapshot.warmup != 5:
        raise BusinessReportError("PR8 快照 Dataset、warmup 或 repeat 不符合正式口径")
    if snapshot.global_summary.sample_count != expected_samples or len(snapshot.cases) != expected_tasks:
        raise BusinessReportError(f"PR8 {mode} 快照样本数或任务数不符合正式口径")
    if not snapshot.git_commit or snapshot.git_commit == "unknown" or not snapshot.samples_path:
        raise BusinessReportError(f"PR8 {mode} 快照缺少提交或原始样本引用")
    required_environment = {"config_hash"}
    if mode == "fixture":
        required_environment.update({"python_version", "platform"})
    else:
        required_environment.update({"provider", "model", "judge_provider", "judge_model", "temperature", "judge_temperature", "timeout_seconds", "retry_count"})
    if not required_environment.issubset(snapshot.environment):
        raise BusinessReportError(f"PR8 {mode} 快照缺少环境或模型配置")
    if set(snapshot.fixture_fingerprints) != {case.case_id for case in snapshot.cases} or any(not value for value in snapshot.fixture_fingerprints.values()):
        raise BusinessReportError(f"PR8 {mode} 快照 Fixture 指纹与任务不一致")


def generate_final_evidence_report(output: Path, manifest_path: Path, fixture_snapshot: BenchmarkSnapshot, ext_snapshot: BenchmarkSnapshot, fixture_samples: Sequence[BenchmarkSample], ext_samples: Sequence[BenchmarkSample]) -> Path:
    """仅在完整证据资格满足后生成跨 PR 报告，拒绝生成 README/简历孤立数字。"""
    manifest = validate_final_evidence(manifest_path, fixture_snapshot, ext_snapshot)
    _validate_final_samples(fixture_samples, fixture_snapshot, "fixture", expected_samples=300)
    _validate_final_samples(ext_samples, ext_snapshot, "ext", expected_samples=180)
    fixture = summarize_business_samples(fixture_samples, "fixture")
    ext = summarize_business_samples(ext_samples, "ext")
    if fixture["sample_count"] != 300 or ext["sample_count"] != 180:
        raise BusinessReportError("正式收口要求 Fixture 300 条及 EXT 180 条样本")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "evidence-summary.md"
    if path.exists():
        raise FileExistsError("最终证据报告已存在，拒绝覆盖")
    path.write_text("# PR1 至 PR8 正式证据收口\n\n" + json.dumps({"manifest_entries": len(manifest["entries"]), "fixture": fixture, "ext": ext}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _validate_final_samples(samples: Sequence[BenchmarkSample], snapshot: BenchmarkSnapshot, mode: str, *, expected_samples: int) -> None:
    """交叉验证正式 JSONL 派生样本与快照，拒绝模式、提交或配置混入。"""
    formal = [sample for sample in samples if not sample.is_warmup]
    if len(formal) != expected_samples or any(sample.execution_mode != mode for sample in formal):
        raise BusinessReportError(f"PR8 {mode} 正式样本数或执行模式不符合口径")
    if any(sample.formal_sampling is not True for sample in formal):
        raise BusinessReportError(f"PR8 {mode} 样本未明确标记为正式采样")
    if any(sample.git_commit != snapshot.git_commit or sample.config_hash != snapshot.environment["config_hash"] for sample in formal):
        raise BusinessReportError(f"PR8 {mode} 样本混入不同提交或配置")
    if any(sample.fixture_fingerprint != snapshot.fixture_fingerprints.get(sample.case_id) for sample in formal):
        raise BusinessReportError(f"PR8 {mode} 样本 Fixture 指纹与快照不一致")
    if mode == "ext":
        if any(sample.provider != snapshot.environment["provider"] or sample.model != snapshot.environment["model"] or sample.judge_provider != snapshot.environment["judge_provider"] or sample.judge_model != snapshot.environment["judge_model"] for sample in formal):
            raise BusinessReportError("PR8 EXT 样本混入不同 Provider、模型或 Judge 条件")


def render_partial_report(fixture: Mapping[str, object], ext: Mapping[str, object] | None = None) -> str:
    """生成开发期局部报告；明确不含 README/简历正式结论。"""
    lines = ["# PR8 业务基线局部报告", "", f"Fixture 任务数：{fixture['task_count']}，正式样本：{fixture['sample_count']}。"]
    if ext is not None:
        lines.append(f"[EXT] 任务数：{ext['task_count']}，正式样本：{ext['sample_count']}，进入 Judge：{ext['entered_judge']}。")
    lines.extend(["", "本报告不代表线上用户成功率、模型通用能力或真实外部工具安全性；缺少完整正式证据时不得生成 README 或简历数字。"])
    return "\n".join(lines) + "\n"
