"""PR7 快照资格校验测试。"""

import pytest

from benchmarks.delegation_reliability import _outcome_sample, _validate_snapshot_samples
from benchmarks.delegation_workloads import ChildOutcome, DelegationWorkloadConfig
from .helpers import make_sample


def test_missing_required_scenario_rejected() -> None:
    """缺少三类必需场景时不得生成正式快照。"""
    config = DelegationWorkloadConfig(outcome_repeat=1, cancellation_repeat=1, concurrent_parents=1, concurrent_repeat=1)
    samples = [make_sample(case_id="completed", formal_sampling=True, fixture_version="f", environment={"python_version": "3", "platform": "x"})]
    with pytest.raises(ValueError, match="正式样本数"):
        _validate_snapshot_samples(samples, config)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", tuple(ChildOutcome))
async def test_outcome_samples_include_real_zero_isolation_counts(tmp_path, outcome: ChildOutcome) -> None:
    """四种子终态的统一正式采样必须带有可判定的隔离事实。"""
    sample = await _outcome_sample(tmp_path / outcome.value, outcome, 0, False, DelegationWorkloadConfig(fake_delay_ms=1))
    assert sample.passed is True
    assert sample.assertions_passed == sample.assertions_total
    assert sample.cross_chain_message_count == 0
    assert sample.cross_chain_context_count == 0
    assert sample.cross_chain_tool_count == 0
    assert sample.cross_chain_stream_count == 0
    assert sample.misdelivery_count == 0
