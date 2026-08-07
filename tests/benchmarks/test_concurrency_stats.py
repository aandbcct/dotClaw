"""concurrency_stats 模块测试。"""

from __future__ import annotations

from benchmarks.concurrency_stats import (
    ConcurrencyLatencyStats,
    ScenarioStats,
    aggregate_scenario_stats,
    compare_schedule_modes,
    compute_change_rate,
    compute_throughput,
)
from benchmarks.eval_baseline_models import BenchmarkSample, ConcurrencyScenario, ScheduleMode


def _make_concurrency_sample(**overrides) -> BenchmarkSample:
    """构造一条带并发字段的 BenchmarkSample。"""
    base = dict(
        dataset="reliability_concurrency",
        case_id="fifo_same_session",
        attempt=0,
        is_warmup=False,
        git_commit="abc123",
        python_version="3.13.5",
        platform="Windows",
        config_hash="",
        eval_schema_version="",
        passed=True,
        failure_kind=None,
        assertions_passed=1,
        assertions_total=1,
        trace_available=False,
        wall_duration_ms=50.0,
        run_id="run-1",
        queue_wait_ms=10.0,
        schedule_mode=ScheduleMode.SESSION_LOCK,
        session_count=1,
        requests_per_session=20,
        fake_delay_ms=20,
        accepted_seq=1,
        execution_started_seq=1,
        completed_seq=1,
        conversation_commit_seq=1,
        message_leak_count=0,
        event_leak_count=0,
        context_leak_count=0,
        tool_leak_count=0,
        stream_leak_count=0,
        cancel_delivery_ms=None,
        cancel_effect_ms=None,
        cancellation_delivered=None,
        cancellation_effective=None,
        lock_released=None,
        followup_completed=None,
        evidence_summary=None,
    )
    base.update(overrides)
    return BenchmarkSample(**base)


class TestConcurrencyLatencyStats:
    """时延分布测试。"""

    def test_from_values_normal(self):
        """正常数据。"""
        stats = ConcurrencyLatencyStats.from_values([10.0, 20.0, 30.0, 40.0, 50.0])
        assert stats.sample_count == 5
        assert stats.p50_ms == 30.0
        assert stats.max_ms == 50.0

    def test_from_values_empty(self):
        """空数据。"""
        stats = ConcurrencyLatencyStats.from_values([])
        assert stats.sample_count == 0
        assert stats.p50_ms == 0.0

    def test_from_values_single(self):
        """单样本。"""
        stats = ConcurrencyLatencyStats.from_values([42.0])
        assert stats.sample_count == 1
        assert stats.p50_ms == 42.0

    def test_to_dict_roundtrip(self):
        """序列化往返。"""
        stats = ConcurrencyLatencyStats.from_values([10.0, 20.0, 30.0])
        d = stats.to_dict()
        assert d["sample_count"] == 3
        assert d["p50_ms"] == 20.0


class TestComputeThroughput:
    """吞吐量计算测试。"""

    def test_normal(self):
        """正常计算。"""
        tput = compute_throughput(100, 1000.0)
        assert tput == 100.0  # 100 req / 1s

    def test_zero_time(self):
        """零时间。"""
        tput = compute_throughput(100, 0.0)
        assert tput == 0.0

    def test_zero_requests(self):
        """零请求。"""
        tput = compute_throughput(0, 1000.0)
        assert tput == 0.0


class TestComputeChangeRate:
    """变化率计算测试。"""

    def test_improvement(self):
        """Session 锁更快。"""
        rate = compute_change_rate(10.0, 100.0)
        assert rate == -0.9  # (10-100)/100

    def test_degradation(self):
        """Session 锁更慢。"""
        rate = compute_change_rate(200.0, 100.0)
        assert rate == 1.0

    def test_zero_baseline(self):
        """0 基线不可比。"""
        rate = compute_change_rate(10.0, 0.0)
        assert rate is None


class TestAggregateScenarioStats:
    """场景统计聚合测试。"""

    def test_basic_aggregation(self):
        """基本聚合。"""
        samples = [
            _make_concurrency_sample(wall_duration_ms=10.0, queue_wait_ms=2.0),
            _make_concurrency_sample(wall_duration_ms=20.0, queue_wait_ms=3.0),
            _make_concurrency_sample(wall_duration_ms=30.0, queue_wait_ms=5.0),
        ]
        stats = aggregate_scenario_stats("fifo", "session_lock", samples, 1, ConcurrencyScenario.FIFO_SAME_SESSION)
        assert stats.total_requests == 3
        assert stats.wall_duration_ms.sample_count == 3
        assert stats.queue_wait_ms.p50_ms == 3.0

    def test_warmup_excluded(self):
        """warmup 样本应被过滤。"""
        samples = [
            _make_concurrency_sample(is_warmup=True, wall_duration_ms=10.0),
            _make_concurrency_sample(wall_duration_ms=20.0),
        ]
        stats = aggregate_scenario_stats("fifo", "session_lock", samples, 1, ConcurrencyScenario.FIFO_SAME_SESSION)
        assert stats.total_requests == 1

    def test_zero_queue_wait_is_included_in_distribution(self):
        """零等待是有效测量值，必须参与排队分位数计算。"""
        samples = [
            _make_concurrency_sample(queue_wait_ms=0.0),
            _make_concurrency_sample(queue_wait_ms=10.0),
        ]
        stats = aggregate_scenario_stats("queue", "session_lock", samples, 1, ConcurrencyScenario.FIFO_SAME_SESSION)
        assert stats.queue_wait_ms.sample_count == 2
        assert stats.queue_wait_ms.p50_ms == 5.0

    def test_cancel_stats(self):
        """取消场景统计。"""
        samples = [
            _make_concurrency_sample(
                case_id=ConcurrencyScenario.CANCEL_NON_BLOCKING.value,
                cancel_delivery_ms=5.0, cancel_effect_ms=50.0,
                cancellation_delivered=True, cancellation_effective=True,
                lock_released=True, followup_completed=True,
            ),
            _make_concurrency_sample(
                case_id=ConcurrencyScenario.CANCEL_NON_BLOCKING.value,
                cancel_delivery_ms=10.0, cancel_effect_ms=100.0,
                cancellation_delivered=True, cancellation_effective=True,
                lock_released=True, followup_completed=True,
            ),
        ]
        stats = aggregate_scenario_stats("cancel", "session_lock", samples, 1, ConcurrencyScenario.CANCEL_NON_BLOCKING)
        assert stats.cancel_passed_count == 2

    def test_leak_totals(self):
        """泄漏条数与通过请求数必须采用独立口径。"""
        samples = [
            _make_concurrency_sample(case_id=ConcurrencyScenario.MULTI_SESSION_ISOLATION.value, message_leak_count=2, event_leak_count=1),
            _make_concurrency_sample(case_id=ConcurrencyScenario.MULTI_SESSION_ISOLATION.value, message_leak_count=1, event_leak_count=0),
        ]
        stats = aggregate_scenario_stats("isolation", "session_lock", samples, 1, ConcurrencyScenario.MULTI_SESSION_ISOLATION)
        assert stats.message_leak_total == 3
        assert stats.event_leak_total == 1
        assert stats.isolation_passed_count == 0
        assert stats.isolation_total_count == 2

    def test_fifo_failed_sample_cannot_count_as_passed(self):
        """顺序字段即使相等，最终失败的请求也不得计入 FIFO 通过数。"""
        samples = [_make_concurrency_sample(passed=False, failure_kind="runtime")]
        stats = aggregate_scenario_stats("fifo", "session_lock", samples, 1, ConcurrencyScenario.FIFO_SAME_SESSION)
        assert stats.fifo_passed_count == 0
        assert stats.fifo_total_count == 1

    def test_non_isolation_samples_do_not_expand_isolation_denominator(self):
        """没有隔离证据字段的场景不得计入隔离通过分母。"""
        samples = [_make_concurrency_sample(
            case_id=ConcurrencyScenario.SESSION_SCALING.value,
            message_leak_count=None, event_leak_count=None, context_leak_count=None,
            tool_leak_count=None, stream_leak_count=None,
        )]
        stats = aggregate_scenario_stats("scaling", "session_lock", samples, 1, ConcurrencyScenario.SESSION_SCALING)
        assert stats.isolation_total_count == 0
        assert stats.isolation_passed_count == 0


class TestCompareScheduleModes:
    """调度模式对照测试。"""

    def test_basic_comparison(self):
        """基本对照。"""
        session = ScenarioStats(
            scenario_id="fixed", schedule_mode="session_lock",
            total_requests=32, total_batches=10,
            queue_wait_ms=ConcurrencyLatencyStats.from_values([10.0]),
            wall_duration_ms=ConcurrencyLatencyStats.from_values([50.0]),
            cancel_delivery_ms=ConcurrencyLatencyStats.from_values([]),
            cancel_effect_ms=ConcurrencyLatencyStats.from_values([]),
            fifo_passed_count=10, fifo_total_count=10,
            isolation_passed_count=10, isolation_total_count=10,
            cancel_passed_count=0, cancel_total_count=0,
            message_leak_total=0, event_leak_total=0,
            context_leak_total=0, tool_leak_total=0, stream_leak_total=0,
            throughput_per_sec=100.0, batch_total_ms=320.0,
        )
        global_stats = ScenarioStats(
            scenario_id="fixed", schedule_mode="global_lock",
            total_requests=32, total_batches=10,
            queue_wait_ms=ConcurrencyLatencyStats.from_values([50.0]),
            wall_duration_ms=ConcurrencyLatencyStats.from_values([200.0]),
            cancel_delivery_ms=ConcurrencyLatencyStats.from_values([]),
            cancel_effect_ms=ConcurrencyLatencyStats.from_values([]),
            fifo_passed_count=10, fifo_total_count=10,
            isolation_passed_count=10, isolation_total_count=10,
            cancel_passed_count=0, cancel_total_count=0,
            message_leak_total=0, event_leak_total=0,
            context_leak_total=0, tool_leak_total=0, stream_leak_total=0,
            throughput_per_sec=20.0, batch_total_ms=1600.0,
        )
        comparison = compare_schedule_modes(session, global_stats)
        assert "throughput_change_rate" in comparison
        assert "note" in comparison
        # (100 - 20) / 20 = 4.0
        assert comparison["throughput_change_rate"] == 4.0
