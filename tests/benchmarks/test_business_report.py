"""PR8 业务报告分区统计测试。"""

from dataclasses import replace

from benchmarks.business_report import summarize_business_samples
from .helpers import make_sample


def test_fixture_summary_excludes_warmup_and_ext() -> None:
    """Fixture 汇总不混入预热或 EXT 样本。"""
    base = make_sample(case_id="business", wall_duration_ms=10.0)
    fixture = replace(base, execution_mode="fixture", task_category="workspace", deterministic_passed=True, llm_call_count=1, tool_call_count=2)
    warmup = replace(fixture, is_warmup=True)
    ext = replace(fixture, execution_mode="ext")
    summary = summarize_business_samples([fixture, warmup, ext], "fixture")
    assert summary["sample_count"] == 1 and summary["task_count"] == 1
