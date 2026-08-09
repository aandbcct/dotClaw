"""PR8 业务报告分区统计测试。"""

import json
from dataclasses import replace

import pytest

from benchmarks.business_report import BusinessReportError, generate_final_evidence_report, main as report_main, summarize_business_samples, validate_final_evidence
from benchmarks.eval_baseline_models import BenchmarkSample
from .helpers import make_sample


def test_fixture_summary_excludes_warmup_and_ext() -> None:
    """Fixture 汇总不混入预热或 EXT 样本。"""
    base = make_sample(case_id="business", wall_duration_ms=10.0)
    fixture = replace(base, execution_mode="fixture", task_category="workspace", task_kind="standard_case", deterministic_passed=True, llm_call_count=1, tool_call_count=2)
    warmup = replace(fixture, is_warmup=True)
    ext = replace(fixture, execution_mode="ext")
    summary = summarize_business_samples([fixture, warmup, ext], "fixture")
    assert summary["sample_count"] == 1 and summary["task_count"] == 1
    assert summary["completion_wilson_95"][0] <= 1.0


def test_ext_summary_counts_failed_judge_criteria_by_task_and_category() -> None:
    """EXT 报告必须保留 Judge 逐项失败数，且任务/类别汇总带分母。"""
    base = make_sample(execution_mode="ext", task_category="research", task_kind="standard_case", deterministic_passed=True)
    failed = replace(base, passed=False, judge_verdict="fail", judge_criteria={"facts": "fail", "boundary": "pass"}, failure_attribution="judge_quality_failure")
    passed = replace(base, case_id="research-2", judge_verdict="pass", judge_criteria={"facts": "pass", "boundary": "pass"})
    summary = summarize_business_samples([failed, passed], "ext")
    assert summary["entered_judge"] == 2 and summary["quality_passed"] == 1
    assert summary["judge_criterion_failures"] == {"facts": 1}
    assert summary["by_task"]["tool_success"]["sample_count"] == 1
    assert summary["by_category"]["research"]["sample_count"] == 2


def test_final_evidence_rejects_missing_manifest(tmp_path) -> None:
    """缺少 PR1 至 PR7 清单时绝不生成跨 PR 结论。"""
    from benchmarks.eval_baseline_stats import build_snapshot
    snapshot = build_snapshot(snapshot_id="x", generated_at="x", git_commit="x", dataset="runtime_core_v2", environment={}, warmup=0, repeat=30, samples=[replace(make_sample(), dataset="runtime_core_v2")], samples_path="samples/x.jsonl", samples_content_summary={})
    with pytest.raises(BusinessReportError, match="缺少 PR1 至 PR7"):
        validate_final_evidence(tmp_path / "missing.json", snapshot, snapshot)


def test_final_evidence_rejects_manifest_entry_without_traceability(tmp_path) -> None:
    """七条空壳条目不得冒充 PR1 至 PR7 的可消费正式证据。"""
    from benchmarks.eval_baseline_stats import build_snapshot
    snapshot = build_snapshot(snapshot_id="x", generated_at="x", git_commit="x", dataset="runtime_core_v2", environment={}, warmup=0, repeat=30, samples=[replace(make_sample(), dataset="runtime_core_v2")], samples_path="samples/x.jsonl", samples_content_summary={})
    manifest = {"entries": [{"suite": f"pr{index}"} for index in range(1, 8)]}
    path = tmp_path / "manifest.json"
    path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    with pytest.raises(BusinessReportError, match="缺少资格字段"):
        validate_final_evidence(path, snapshot, snapshot)


def test_final_evidence_report_accepts_only_consistent_full_evidence_set(tmp_path) -> None:
    """合成完整证据集仅验证收口契约，不代表执行过正式采样。"""
    from benchmarks.eval_baseline_stats import build_snapshot

    def samples(mode: str, task_ids: list[str], category: str) -> list[BenchmarkSample]:
        return [
            replace(
                make_sample(case_id=task_id, attempt=attempt, dataset="runtime_core_v2", git_commit="commit-x", config_hash="cfg-x"),
                execution_mode=mode,
                task_category=category,
                task_kind="standard_case",
                deterministic_passed=True,
                fixture_fingerprint=f"fixture-{task_id}",
                formal_sampling=True,
                provider="provider-x" if mode == "ext" else None,
                model="model-x" if mode == "ext" else None,
                judge_provider="provider-x" if mode == "ext" else None,
                judge_model="judge-x" if mode == "ext" else None,
                judge_verdict="pass" if mode == "ext" else None,
                judge_criteria={"quality": "pass"} if mode == "ext" else None,
            )
            for task_id in task_ids
            for attempt in range(30)
        ]

    fixture_samples = samples("fixture", [f"fixture-{index}" for index in range(10)], "workspace")
    ext_samples = samples("ext", [f"ext-{index}" for index in range(6)], "research")
    fixture_snapshot = build_snapshot(snapshot_id="fixture", generated_at="now", git_commit="commit-x", dataset="runtime_core_v2", environment={"config_hash": "cfg-x", "python_version": "3.13", "platform": "Windows", "formal_sampling": "true"}, warmup=5, repeat=30, samples=fixture_samples, samples_path="samples/fixture.jsonl", samples_content_summary={"line_count": 300})
    ext_snapshot = build_snapshot(snapshot_id="ext", generated_at="now", git_commit="commit-x", dataset="runtime_core_v2", environment={"config_hash": "cfg-x", "provider": "provider-x", "model": "model-x", "temperature": "0.0", "judge_provider": "provider-x", "judge_model": "judge-x", "judge_temperature": "0.0", "timeout_seconds": "60.0", "retry_count": "0", "formal_sampling": "true"}, warmup=5, repeat=30, samples=ext_samples, samples_path="samples/ext.jsonl", samples_content_summary={"line_count": 180})
    entries = [{"snapshot": f"snapshot-{index}.json", "suite": f"suite-{index}", "git_commit": "commit-x", "samples_path": f"samples-{index}.jsonl", "formal_sample_count": 1, "fixture_fingerprints": {"case": "fixture"}, "environment": {"python_version": "3.13", "platform": "Windows", "config_hash": "cfg-x", "eval_schema_version": "1.0"}} for index in range(7)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(__import__("json").dumps({"entries": entries}), encoding="utf-8")
    path = generate_final_evidence_report(tmp_path / "report", manifest, fixture_snapshot, ext_snapshot, fixture_samples, ext_samples)
    assert path.is_file() and "PR1 至 PR8" in path.read_text(encoding="utf-8")
    with pytest.raises(BusinessReportError, match="未明确标记为正式采样"):
        validate_final_evidence(manifest, replace(fixture_snapshot, environment={**fixture_snapshot.environment, "formal_sampling": "false"}), ext_snapshot)

    # 以实际快照相对路径写出 JSONL，验证收口 CLI 不依赖内存合成对象。
    fixture_root, ext_root = tmp_path / "fixture-artifacts", tmp_path / "ext-artifacts"
    for root, snapshot, samples in ((fixture_root, fixture_snapshot, fixture_samples), (ext_root, ext_snapshot, ext_samples)):
        samples_path = root / snapshot.samples_path
        samples_path.parent.mkdir(parents=True)
        samples_path.write_text("\n".join(json.dumps(sample.to_dict(), ensure_ascii=False) for sample in samples) + "\n", encoding="utf-8")
        (root / f"{snapshot.snapshot_id}.json").write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "report-from-files"
    assert report_main(["--manifest", str(manifest), "--fixture-snapshot", str(fixture_root / "fixture.json"), "--ext-snapshot", str(ext_root / "ext.json"), "--output", str(output)]) == 0
    assert (output / "evidence-summary.md").is_file()
