"""PR6 场景编排与工件写出测试。"""

import asyncio

from benchmarks.context_reliability import ContextRunConfig, run_context_suite, write_artifacts


def test_context_suite_emits_all_required_scenarios(tmp_path) -> None:
    """最小 repeat 必须从真实 Runtime 事实产生完整 PR6 场景。"""
    config = ContextRunConfig(recovery_warmup=0, recovery_repeat=1, performance_warmup=0, performance_repeat=1, boundary_repeat=1)
    samples = asyncio.run(run_context_suite(config))
    by_case = {item.case_id: item for item in samples}
    assert by_case["fixed_input_consistency"].passed is True
    recovery = by_case["cold_recovery_v1_to_v2"]
    assert recovery.passed is True
    assert recovery.same_context_version is True
    assert recovery.context_drift_count == 0
    assert recovery.provider_reload_count == 0
    assert recovery.conversation_count_delta == 1
    assert recovery.run_message_count_delta == 1
    assert recovery.run_event_count_delta is not None and recovery.run_event_count_delta > 0
    assert recovery.git_commit
    assert recovery.config_hash
    assert recovery.eval_schema_version == "2.0"
    assert by_case["replay_efficiency_replay"].provider_load_count == 0
    assert by_case["replay_efficiency_forced"].provider_load_count == 1
    assert by_case["replay_efficiency_replay"].recovery_stage_duration_ms is not None
    assert by_case["replay_efficiency_forced"].recovery_stage_duration_ms is not None
    for outcome in ("success", "failure", "cancelled", "abandoned"):
        sample = by_case[f"compression_{outcome}"]
        assert sample.passed is True
        assert sample.tokens_before is not None and sample.tokens_after is not None
        assert sample.tokens_after < sample.tokens_before
        assert sample.tool_pair_break_count == 0
        assert sample.session_pollution_count == 0
    assert by_case["compression_success"].session_projection_count == 1
    for owner in ("global", "agent", "session", "run"):
        assert by_case[f"owner_isolation_{owner}"].passed is True
    output = tmp_path / "out"
    write_artifacts(samples, config, output, None)
    assert (output / "context-config.json").exists()
    assert (output / "recovery-replay.md").exists()
    snapshots = list(output.glob("*.json"))
    assert len(snapshots) == 2
    snapshot = next(path for path in snapshots if path.name != "context-config.json")
    import json
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["samples_path"].startswith("samples/")
    assert payload["replay_control"]["snapshot_provider_load_count"] == 0
    assert payload["replay_control"]["forced_provider_load_count"] == 1
    assert payload["compression"]["tool_pair_break_count"] == 0
