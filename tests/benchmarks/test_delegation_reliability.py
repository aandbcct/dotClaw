"""PR7 快照资格校验测试。"""

import pytest

from benchmarks.delegation_reliability import _validate_snapshot_samples
from benchmarks.delegation_workloads import DelegationWorkloadConfig
from .helpers import make_sample


def test_missing_required_scenario_rejected() -> None:
    """缺少三类必需场景时不得生成正式快照。"""
    config = DelegationWorkloadConfig(outcome_repeat=1, cancellation_repeat=1, concurrent_parents=1, concurrent_repeat=1)
    samples = [make_sample(case_id="completed", formal_sampling=True, fixture_version="f", environment={"python_version": "3", "platform": "x"})]
    with pytest.raises(ValueError, match="正式样本数"):
        _validate_snapshot_samples(samples, config)
