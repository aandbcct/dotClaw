"""PR6 场景编排与工件写出测试。"""

import asyncio

from benchmarks.context_reliability import ContextRunConfig, run_context_suite, write_artifacts


def test_context_suite_emits_all_required_scenarios(tmp_path) -> None:
    """最小 repeat 覆盖一致性、恢复、压缩、Owner 与对照记录。"""
    config = ContextRunConfig(recovery_warmup=0, recovery_repeat=1, performance_warmup=0, performance_repeat=1)
    samples = asyncio.run(run_context_suite(config))
    assert any(item.case_id == "cold_recovery_v1_to_v2" for item in samples)
    assert any(item.case_id.startswith("owner_isolation_") for item in samples)
    output = tmp_path / "out"
    write_artifacts(samples, config, output, None)
    assert (output / "context-config.json").exists()
    assert (output / "recovery-replay.md").exists()
    assert len(list(output.glob("*.json"))) == 2
