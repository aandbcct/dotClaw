"""PR8 业务报告分区统计测试。"""

from dataclasses import replace

import pytest

from benchmarks.business_report import BusinessReportError, summarize_business_samples, validate_final_evidence
from .helpers import make_sample


def test_fixture_summary_excludes_warmup_and_ext() -> None:
    """Fixture 汇总不混入预热或 EXT 样本。"""
    base = make_sample(case_id="business", wall_duration_ms=10.0)
    fixture = replace(base, execution_mode="fixture", task_category="workspace", deterministic_passed=True, llm_call_count=1, tool_call_count=2)
    warmup = replace(fixture, is_warmup=True)
    ext = replace(fixture, execution_mode="ext")
    summary = summarize_business_samples([fixture, warmup, ext], "fixture")
    assert summary["sample_count"] == 1 and summary["task_count"] == 1
    assert summary["completion_wilson_95"][0] <= 1.0


def test_final_evidence_rejects_missing_manifest(tmp_path) -> None:
    """缺少 PR1 至 PR7 清单时绝不生成跨 PR 结论。"""
    from benchmarks.eval_baseline_stats import build_snapshot
    snapshot = build_snapshot(snapshot_id="x", generated_at="x", git_commit="x", dataset="runtime_core_v2", environment={}, warmup=0, repeat=30, samples=[replace(make_sample(), dataset="runtime_core_v2")], samples_path="samples/x.jsonl", samples_content_summary={})
    with pytest.raises(BusinessReportError, match="缺少 PR1 至 PR7"):
        validate_final_evidence(tmp_path / "missing.json", snapshot, snapshot)
