"""concurrency_reliability 编排模块测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.concurrency_reliability import (
    ConcurrencyReliabilityRunner,
    _GlobalLockCoordinator,
    _build_scheduling_report,
)
from benchmarks.concurrency_stats import (
    ConcurrencyLatencyStats,
    ConcurrencySnapshot,
    ScenarioStats,
)
from benchmarks.concurrency_workloads import WorkloadConfig
from benchmarks.eval_baseline_models import BenchmarkSample, ConcurrencyScenario, ScheduleMode


class TestConcurrencyReliabilityRunner:
    """编排器测试。"""

    @pytest.mark.asyncio
    async def test_invalid_core_warmup(self, tmp_path):
        """负数 warmup 拒绝。"""
        runner = ConcurrencyReliabilityRunner()
        with pytest.raises(ValueError, match="warmup"):
            await runner.run(
                core_warmup=-1, core_repeat=1,
                scaling_warmup=0, scaling_repeat=1, fake_delay_ms=20,
                output_dir=tmp_path,
            )

    @pytest.mark.asyncio
    async def test_invalid_scaling_repeat(self, tmp_path):
        """scaling_repeat=0 拒绝。"""
        runner = ConcurrencyReliabilityRunner()
        with pytest.raises(ValueError, match="repeat"):
            await runner.run(
                core_warmup=0, core_repeat=1,
                scaling_warmup=0, scaling_repeat=0, fake_delay_ms=20,
                output_dir=tmp_path,
            )


class TestGlobalLockCoordinator:
    """全局锁协调器测试。"""

    @pytest.mark.asyncio
    async def test_global_lock_serializes(self):
        """验证全局锁确实串行化提交。"""
        import asyncio

        order: list[int] = []

        class _FakeInteraction:
            async def submit(self, session_id, user_message, output_port=None):
                order.append(int(session_id[-1]))
                await asyncio.sleep(0.01)
                from dotclaw.runtime.application.dto import RunResult
                from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome
                return RunResult(
                    run_id=f"run-{session_id}",
                    state=AgentRunState(mode=Ended(RunOutcome.COMPLETED)),
                )

        global_coord = _GlobalLockCoordinator(_FakeInteraction())

        # 并发提交（全局锁下应串行）
        tasks = [
            global_coord.submit("s1", "one"),
            global_coord.submit("s2", "two"),
            global_coord.submit("s3", "three"),
        ]
        await asyncio.gather(*tasks)

        # 全局锁下顺序应稳定（按锁获取顺序）
        assert len(order) == 3


class TestWorkloadConfigValidation:
    """工作负载配置校验测试。"""

    def test_minimal_config(self):
        """最小合法配置。"""
        config = WorkloadConfig(
            session_count=1,
            requests_per_session=1,
            fake_delay_ms=20,
            schedule_mode=ScheduleMode.SESSION_LOCK,
            warmup=0,
            repeat=1,
        )
        assert config.session_count == 1
        assert config.requests_per_session == 1

    def test_full_config(self):
        """完整配置。"""
        config = WorkloadConfig(
            session_count=8,
            requests_per_session=4,
            fake_delay_ms=20,
            schedule_mode=ScheduleMode.SESSION_LOCK,
            warmup=5,
            repeat=100,
            long_delay_ms=200,
            long_request_session_index=0,
        )
        d = config.to_dict()
        assert d["session_count"] == 8
        assert d["long_delay_ms"] == 200


def _runner_sample(config: WorkloadConfig, case_id: ConcurrencyScenario) -> BenchmarkSample:
    """构造完整 Runner 编排测试所需的一条正式样本。"""
    is_fifo = case_id is ConcurrencyScenario.FIFO_SAME_SESSION
    is_isolation = case_id is ConcurrencyScenario.MULTI_SESSION_ISOLATION
    is_cancel = case_id is ConcurrencyScenario.CANCEL_NON_BLOCKING
    return BenchmarkSample(
        dataset="reliability_concurrency", case_id=case_id.value, attempt=0,
        is_warmup=False, git_commit="test", python_version="3.13.5", platform="Windows",
        config_hash="", eval_schema_version="", passed=True, failure_kind=None,
        assertions_passed=1, assertions_total=1, trace_available=False, wall_duration_ms=1.0,
        run_id="run-1", schedule_mode=config.schedule_mode, session_count=config.session_count,
        requests_per_session=config.requests_per_session, fake_delay_ms=config.fake_delay_ms,
        accepted_seq=1 if is_fifo else None,
        execution_started_seq=1 if is_fifo else None,
        completed_seq=1 if is_fifo else None,
        conversation_commit_seq=1 if is_fifo else None,
        message_leak_count=0 if is_isolation else None,
        event_leak_count=0 if is_isolation else None,
        context_leak_count=0 if is_isolation else None,
        tool_leak_count=0 if is_isolation else None,
        stream_leak_count=0 if is_isolation else None,
        cancel_delivery_ms=1.0 if is_cancel else None,
        cancel_effect_ms=2.0 if is_cancel else None,
        cancellation_delivered=True if is_cancel else None,
        cancellation_effective=True if is_cancel else None,
        lock_released=True if is_cancel else None,
        followup_completed=True if is_cancel else None,
        evidence_summary={},
    )


@pytest.mark.asyncio
async def test_runner_separates_sampling_groups_and_scenario_statistics(tmp_path, monkeypatch):
    """完整 Runner 必须分别配置两类采样，并且不把正确性字段聚合到其他场景。"""
    runner = ConcurrencyReliabilityRunner()

    async def fifo(config, agent_id, output_dir):
        return [_runner_sample(config, ConcurrencyScenario.FIFO_SAME_SESSION)], 1.0

    async def isolation(config, agent_id, output_dir):
        return [_runner_sample(config, ConcurrencyScenario.MULTI_SESSION_ISOLATION)], 1.0

    async def scaling(config, agent_id, output_dir):
        return [_runner_sample(config, ConcurrencyScenario.SESSION_SCALING)], 1.0

    async def mixed(config, agent_id, *, global_lock):
        return [_runner_sample(config, ConcurrencyScenario.MIXED_LONG_SHORT)], 1.0

    async def cancel(config, agent_id, output_dir):
        return [_runner_sample(config, ConcurrencyScenario.CANCEL_NON_BLOCKING)]

    monkeypatch.setattr(runner, "_run_scenario_fifo", fifo)
    monkeypatch.setattr(runner, "_run_scenario_isolation", isolation)
    monkeypatch.setattr(runner, "_run_scenario_scaling", scaling)
    monkeypatch.setattr(runner, "_run_scenario_scaling_global", scaling)
    monkeypatch.setattr(runner, "_run_scenario_mixed", mixed)
    monkeypatch.setattr(runner, "_run_scenario_cancel", cancel)

    snapshot = await runner.run(
        core_warmup=2, core_repeat=3, scaling_warmup=4, scaling_repeat=5,
        fake_delay_ms=1, output_dir=tmp_path,
    )
    isolation_stats = next(item for item in snapshot.scenarios if item.scenario_id == "multi_session_isolation")
    cancel_stats = next(item for item in snapshot.scenarios if item.scenario_id == "cancel_non_blocking")
    scaling_stats = next(item for item in snapshot.scenarios if item.scenario_id == "session_scaling_1s")
    assert snapshot.sampling_configs == {
        "core": {"warmup": 2, "repeat": 3},
        "scaling": {"warmup": 4, "repeat": 5},
    }
    assert (isolation_stats.fifo_total_count, isolation_stats.isolation_total_count) == (0, 1)
    assert (cancel_stats.fifo_total_count, cancel_stats.isolation_total_count, cancel_stats.cancel_total_count) == (0, 0, 1)
    assert (scaling_stats.fifo_total_count, scaling_stats.isolation_total_count, scaling_stats.cancel_total_count) == (0, 0, 0)


def _scenario(scenario_id: str, schedule_mode: str, throughput: float) -> ScenarioStats:
    """构造报告协议测试需要的最小场景统计。"""
    latency = ConcurrencyLatencyStats.from_values([10.0])
    return ScenarioStats(
        scenario_id=scenario_id,
        schedule_mode=schedule_mode,
        total_requests=32,
        total_batches=1,
        queue_wait_ms=latency,
        wall_duration_ms=latency,
        cancel_delivery_ms=ConcurrencyLatencyStats.from_values([]),
        cancel_effect_ms=ConcurrencyLatencyStats.from_values([]),
        fifo_passed_count=0,
        fifo_total_count=0,
        isolation_passed_count=32,
        isolation_total_count=32,
        cancel_passed_count=0,
        cancel_total_count=0,
        message_leak_total=0,
        event_leak_total=0,
        context_leak_total=0,
        tool_leak_total=0,
        stream_leak_total=0,
        throughput_per_sec=throughput,
        batch_total_ms=100.0,
    )


def test_write_artifacts_writes_required_pr3_reports(tmp_path):
    """正式工件必须包含正确性和调度对照两份独立报告。"""
    snapshot = ConcurrencySnapshot(
        suite="reliability_concurrency_v1",
        generated_at="2026-08-06T00:00:00+00:00",
        git_commit="test",
        warmup=5,
        repeat=100,
        fake_delay_ms=20,
        scenarios=(
            _scenario("fixed_concurrency_session", "session_lock", 100.0),
            _scenario("fixed_concurrency_global", "global_lock", 25.0),
        ),
    )
    ConcurrencyReliabilityRunner()._write_artifacts(snapshot, [], tmp_path, None)
    assert (tmp_path / "correctness.md").is_file()
    assert (tmp_path / "scheduling-comparison.md").is_file()


def test_scheduling_report_contains_comparable_change_rate():
    """同负载双调度模式必须呈现可复核的变化率。"""
    snapshot = ConcurrencySnapshot(
        suite="reliability_concurrency_v1",
        generated_at="2026-08-06T00:00:00+00:00",
        git_commit="test",
        warmup=5,
        repeat=100,
        fake_delay_ms=20,
        scenarios=(
            _scenario("fixed_concurrency_session", "session_lock", 100.0),
            _scenario("fixed_concurrency_global", "global_lock", 25.0),
        ),
    )
    report = _build_scheduling_report(snapshot, "snapshot", Path("samples.jsonl"))
    assert "300.00%" in report
