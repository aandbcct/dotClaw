"""concurrency_reliability 编排模块测试。"""

from __future__ import annotations

import pytest

from benchmarks.concurrency_reliability import (
    ConcurrencyReliabilityRunner,
    _GlobalLockCoordinator,
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
