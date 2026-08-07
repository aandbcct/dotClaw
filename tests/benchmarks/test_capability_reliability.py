"""PR5 安全矩阵端到端冒烟测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.capability_reliability import run_suite, write_artifacts


@pytest.mark.asyncio
async def test_matrix_executes_and_records_barriers() -> None:
    """完整矩阵可执行；明确阻断 Case 的 Handler 进入数必须为零。"""
    matrix = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "reliability_capability_v1" / "matrix.json"
    samples = await run_suite(matrix, performance_warmup=0, performance_repeat=1)
    blocked = [sample for sample in samples if sample.actual_error_code in {"INVALID_ARGUMENTS", "POLICY_DENIED", "APPROVAL_DENIED"}]
    assert blocked and all(sample.handler_entered == 0 for sample in blocked)
    matrix_hashes = {sample.config_hash for sample in samples}
    assert len(matrix_hashes) == 1
    assert next(iter(matrix_hashes)) != "capability-v1-fixed"


@pytest.mark.asyncio
async def test_artifacts_include_matrix_and_overhead_reports(tmp_path: Path) -> None:
    """工件写出 JSONL、矩阵配置与两份独立报告。"""
    matrix = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "reliability_capability_v1" / "matrix.json"
    samples = await run_suite(matrix, performance_warmup=0, performance_repeat=1)
    baseline = tmp_path / "baseline"
    write_artifacts(samples, tmp_path, baseline, matrix)
    assert (tmp_path / "security-matrix.md").is_file()
    assert (tmp_path / "security-chain-overhead.md").is_file()
    assert (tmp_path / "matrix-config.json").is_file()
    assert list(tmp_path.glob("*.json"))
    assert list((baseline / "samples").glob("*.jsonl"))
    assert not list(baseline.glob("*.jsonl"))
