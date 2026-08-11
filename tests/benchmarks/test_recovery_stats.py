"""PR4 分层恢复统计测试。"""

from __future__ import annotations

import pytest

from benchmarks.eval_baseline_models import CapabilityStatus, ExternalEffectStatus, RecoveryFaultScenario
from benchmarks.recovery_stats import aggregate_recovery_scenario, formal_recovery_samples

from .helpers import make_sample


def _sample(**overrides):
    """构造最小合法 PR4 样本。"""
    values = dict(
        fault_scenario=RecoveryFaultScenario.TOOL_AFTER_EFFECT,
        fault_point="execute_tools",
        capability_status=CapabilityStatus.FORMAL,
        control_recovery_pass=True,
        internal_facts_pass=True,
        recovery_wall_duration_ms=10.0,
        external_effect_status=ExternalEffectStatus.DUPLICATE_OBSERVED,
        external_duplicate_count=1,
    )
    values.update(overrides)
    return make_sample(**values)


def test_recovery_summary_keeps_external_duplicate_out_of_internal_rate() -> None:
    """外部重复不改写已经通过的内部事实成功率。"""
    summary = aggregate_recovery_scenario([_sample()])
    assert summary.control_success_rate == 1.0
    assert summary.internal_success_rate == 1.0
    assert summary.external_duplicate_count == 1


def test_boundary_sample_is_excluded_from_formal_rate() -> None:
    """委派等能力边界不得混入正式恢复率。"""
    boundary = _sample(capability_status=CapabilityStatus.BOUNDARY)
    assert formal_recovery_samples([boundary]) == []


def test_formal_sample_missing_layer_assertion_rejected() -> None:
    """正式样本缺少三层判据时拒绝聚合。"""
    with pytest.raises(ValueError, match="缺少控制状态"):
        aggregate_recovery_scenario([_sample(control_recovery_pass=None)])
