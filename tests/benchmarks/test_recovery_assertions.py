"""PR4 恢复事实读取与损坏记录的拒绝测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from benchmarks.eval_baseline_models import BenchmarkSample, BenchmarkSchemaError
from benchmarks.recovery_assertions import collect_recovery_facts
from benchmarks.recovery_faults import audit_delegation_cold_rebuild

from .helpers import make_sample


def test_missing_run_is_not_mapped_to_recovery_success(tmp_path: Path) -> None:
    """缺失 Run（运行）权威事实时，判据读取必须明确失败。"""
    with pytest.raises(ValueError, match="读取不到 Run"):
        asyncio.run(collect_recovery_facts(tmp_path, "missing-session", "missing-run", before_action=None, before_context_count=0))


def test_invalid_external_effect_status_is_rejected() -> None:
    """JSONL（逐行 JSON）中的未知外部副作用状态不得静默变为未重复。"""
    payload = make_sample().to_dict()
    payload["external_effect_status"] = "impossible"
    with pytest.raises(BenchmarkSchemaError, match="external_effect_status"):
        BenchmarkSample.from_dict(payload)


def test_wrong_recovery_boolean_type_is_rejected() -> None:
    """存在但类型错误的恢复字段必须失败，不能按缺失默认。"""
    payload = make_sample().to_dict()
    payload["control_recovery_pass"] = "true"
    with pytest.raises(BenchmarkSchemaError, match="control_recovery_pass"):
        BenchmarkSample.from_dict(payload)


def test_delegation_cold_rebuild_records_real_failure_evidence(tmp_path: Path) -> None:
    """实际父 Run 挂起后冷重建回灌失败，保留事件与错误事实。"""
    evidence = asyncio.run(audit_delegation_cold_rebuild(tmp_path))
    assert evidence["checkpoint_action"] == "suspend"
    assert evidence["replay_outcome"] == "failed"
    assert "子运行不存在" in str(evidence["failure"])
    assert "delegation_submitted" in evidence["event_types"]
