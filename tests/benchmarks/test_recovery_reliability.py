"""PR4 工件写出测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from benchmarks.eval_baseline_models import BenchmarkSample, CapabilityStatus
from benchmarks.recovery_reliability import run_process_sample, run_recovery_sample, write_artifacts


def test_write_artifacts_emits_jsonl_and_layered_report(tmp_path: Path) -> None:
    """开发期样本会输出可追溯 JSONL 与分层报告。"""
    sample = asyncio.run(run_recovery_sample(tmp_path / "store", "tool_before_effect", 0, False))
    output = tmp_path / "output"
    write_artifacts([sample], output)
    snapshots = [path for path in output.glob("*.json") if path.name != "fault-config.json"]
    assert len(snapshots) == 1
    snapshot_id = snapshots[0].stem
    assert (output / "samples" / f"{snapshot_id}.jsonl").is_file()
    assert "控制状态" in (output / "recovery-report.md").read_text(encoding="utf-8")
    assert (output / "capability-boundary.md").is_file()


def test_process_sample_has_distinct_fault_point(tmp_path: Path) -> None:
    """子进程强退必须与同节点受控异常分开聚合。"""
    sample = asyncio.run(run_process_sample(tmp_path, 0, False))
    assert sample.fault_point == "execute_tools_subprocess"


def test_write_artifacts_rejects_mixed_formal_configuration(tmp_path: Path) -> None:
    """不同固定配置的样本不得生成可比较的恢复快照。"""
    first = asyncio.run(run_recovery_sample(tmp_path / "one", "tool_before_effect", 0, False))
    second = asyncio.run(run_recovery_sample(tmp_path / "two", "tool_after_effect", 0, False))
    object.__setattr__(second, "config_hash", "different-config")
    with pytest.raises(ValueError, match="不一致"):
        write_artifacts([first, second], tmp_path / "output")


def test_snapshot_excludes_capability_boundary_from_global_success_rate(tmp_path: Path) -> None:
    """能力边界失败保留在 JSONL（逐行 JSON），但不得污染快照成功率。"""
    formal = asyncio.run(run_recovery_sample(tmp_path / "formal", "tool_before_effect", 0, False))
    boundary = BenchmarkSample.from_dict({
        **formal.to_dict(),
        "case_id": "delegation_cold_rebuild_boundary",
        "passed": False,
        "failure_kind": "capability_boundary",
        "capability_status": CapabilityStatus.BOUNDARY.value,
        "capability_reason": "已知边界",
    })
    output = tmp_path / "output"
    write_artifacts([formal, boundary], output)
    snapshot_path = next(path for path in output.glob("*.json") if path.name != "fault-config.json")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["global_summary"]["sample_count"] == 1
    assert payload["global_summary"]["passed_count"] == 1
