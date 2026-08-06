"""concurrency_workloads 模块测试。"""

from __future__ import annotations

import asyncio

import pytest
from benchmarks.concurrency_workloads import (
    ControlledSubmissionGate,
    FixedDelayLLM,
    FixedDelayTool,
    IdentifierCodec,
    WorkloadConfig,
    make_benchmark_request,
)
from benchmarks.eval_baseline_models import ScheduleMode


class TestIdentifierCodec:
    """标识编码测试。"""

    def test_encode_basic(self):
        """基本编码/解码。"""
        encoded = IdentifierCodec.encode(0, 5)
        assert "s0" in encoded
        assert "r5" in encoded
        assert "req" in encoded

    def test_session_prefix(self):
        """Session 级标识前缀。"""
        assert IdentifierCodec.session_prefix(0) == "s0"
        assert IdentifierCodec.session_prefix(7) == "s7"

    def test_extract_session_indices_normal(self):
        """提取合法 Session 索引。"""
        indices = IdentifierCodec.extract_session_indices("s0_r2_req 请回答 s1_r3_req")
        assert indices == {0, 1}

    def test_extract_session_indices_single(self):
        """仅一个 Session 引用。"""
        indices = IdentifierCodec.extract_session_indices("回答来自: s0_r1_req 请回答")
        assert indices == {0}

    def test_extract_session_indices_no_match(self):
        """无 Session 引用。"""
        indices = IdentifierCodec.extract_session_indices("普通回答内容")
        assert indices == set()

    def test_encode_uniqueness(self):
        """不同参数产生不同标识。"""
        a = IdentifierCodec.encode(0, 0)
        b = IdentifierCodec.encode(0, 1)
        c = IdentifierCodec.encode(1, 0)
        assert a != b
        assert b != c
        assert a != c


class TestControlledSubmissionGate:
    """受控提交闸门测试。"""

    def test_sequential_accepted_seq(self):
        """同一 Session 分配严格递增序号。"""
        gate = ControlledSubmissionGate()
        assert gate.accept("s1") == 1
        assert gate.accept("s1") == 2
        assert gate.accept("s1") == 3

    def test_independent_sessions(self):
        """不同 Session 独立计序。"""
        gate = ControlledSubmissionGate()
        assert gate.accept("s1") == 1
        assert gate.accept("s2") == 1
        assert gate.accept("s1") == 2
        assert gate.accept("s2") == 2

    def test_reset(self):
        """重置后序号归零。"""
        gate = ControlledSubmissionGate()
        gate.accept("s1")
        gate.accept("s1")
        gate.reset()
        assert gate.accept("s1") == 1

    def test_multiple_sessions_after_reset(self):
        """重置后多 Session 均从 1 开始。"""
        gate = ControlledSubmissionGate()
        gate.accept("s1")
        gate.accept("s2")
        gate.reset()
        assert gate.accept("s1") == 1
        assert gate.accept("s2") == 1

    @pytest.mark.asyncio
    async def test_enter_releases_application_entry_in_accepted_order(self):
        """受控闸门必须按接受序号放行协程进入应用入口。"""
        gate = ControlledSubmissionGate()
        sequences = [gate.accept("s1") for _ in range(3)]
        observed: list[int] = []

        async def enter(sequence: int) -> None:
            await gate.enter("s1", sequence)
            observed.append(sequence)
            gate.release_next("s1", sequence)

        await asyncio.gather(*(enter(sequence) for sequence in reversed(sequences)))
        assert observed == [1, 2, 3]


class TestWorkloadConfig:
    """工作负载配置测试。"""

    def test_basic_config(self):
        """基本配置往返。"""
        config = WorkloadConfig(
            session_count=8,
            requests_per_session=4,
            fake_delay_ms=20,
            schedule_mode=ScheduleMode.SESSION_LOCK,
            warmup=5,
            repeat=100,
        )
        d = config.to_dict()
        restored = WorkloadConfig.from_dict(d)
        assert restored.session_count == 8
        assert restored.requests_per_session == 4
        assert restored.fake_delay_ms == 20
        assert restored.schedule_mode == ScheduleMode.SESSION_LOCK

    def test_with_long_delay(self):
        """长延迟配置。"""
        config = WorkloadConfig(
            session_count=1,
            requests_per_session=2,
            fake_delay_ms=20,
            schedule_mode=ScheduleMode.SESSION_LOCK,
            warmup=5,
            repeat=100,
            long_delay_ms=200,
            long_request_session_index=0,
        )
        d = config.to_dict()
        restored = WorkloadConfig.from_dict(d)
        assert restored.long_delay_ms == 200
        assert restored.long_request_session_index == 0

    def test_global_lock_mode(self):
        """全局锁模式配置。"""
        config = WorkloadConfig(
            session_count=8,
            requests_per_session=4,
            fake_delay_ms=20,
            schedule_mode=ScheduleMode.GLOBAL_LOCK,
            warmup=5,
            repeat=100,
        )
        assert config.schedule_mode == ScheduleMode.GLOBAL_LOCK


class TestMakeBenchmarkRequest:
    """请求组装测试。"""

    def test_identifier_format(self):
        """标识格式正确。"""
        identifier, msg = make_benchmark_request("agent-1", 3, 7)
        assert identifier == "s3_r7_req"
        assert "s3_r7_req" in msg

    def test_different_params_unique(self):
        """不同参数产生不同消息。"""
        _, msg1 = make_benchmark_request("agent-1", 0, 0)
        _, msg2 = make_benchmark_request("agent-1", 0, 1)
        assert msg1 != msg2
