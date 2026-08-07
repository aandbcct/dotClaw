"""PR4 工件写出测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from benchmarks.recovery_reliability import run_recovery_sample, write_artifacts


def test_write_artifacts_emits_jsonl_and_layered_report(tmp_path: Path) -> None:
    """开发期样本会输出可追溯 JSONL 与分层报告。"""
    sample = asyncio.run(run_recovery_sample(tmp_path / "store", "tool_before_effect", 0, False))
    output = tmp_path / "output"
    write_artifacts([sample], output)
    assert (output / "recovery-samples.jsonl").is_file()
    assert "控制状态" in (output / "recovery-report.md").read_text(encoding="utf-8")
    assert (output / "capability-boundary.md").is_file()
