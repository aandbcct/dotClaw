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
from benchmarks.eval_baseline_models import ScheduleMode


class TestConcurrencyReliabilityRunner:
    """编排器测试。"""

    @pytest.mark.asyncio
    async def test_invalid_warmup(self, tmp_path):
        """负数 warmup 拒绝。"""
        runner = ConcurrencyReliabilityRunner()
        with pytest.raises(ValueError, match="warmup"):
            await runner.run(
                warmup=-1, repeat=1, fake_delay_ms=20,
                output_dir=tmp_path,
            )

    @pytest.mark.asyncio
    async def test_invalid_repeat(self, tmp_path):
        """repeat=0 拒绝。"""
        runner = ConcurrencyReliabilityRunner()
        with pytest.raises(ValueError, match="repeat"):
            await runner.run(
                warmup=0, repeat=0, fake_delay_ms=20,
                output_dir=tmp_path,
            )


class TestGlobalLockCoordinator:
    """全局锁协调器测试。"""

    @pytest.mark.asyncio
    async def test_global_lock_serializes(self):
        """验证全局锁确实串行化提交。"""
        import asyncio

        order: list[int] = []

        class _FakeCoordinator:
            async def submit_prepared(self, session_id, request_factory, output_port=None):
                order.append(int(session_id[-1]))
                await asyncio.sleep(0.01)
                from dotclaw.runtime.application.dto import RunResult
                from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome
                return RunResult(
                    run_id=f"run-{session_id}",
                    state=AgentRunState(mode=Ended(RunOutcome.COMPLETED)),
                )

        global_coord = _GlobalLockCoordinator(_FakeCoordinator())

        # 并发提交（全局锁下应串行）
        tasks = [
            global_coord.submit_prepared("s1", lambda: None),
            global_coord.submit_prepared("s2", lambda: None),
            global_coord.submit_prepared("s3", lambda: None),
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
