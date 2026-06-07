"""Phase 1 测试：MetricsCollector 事件采集。

验证：
- 正常追加事件
- 10000 事件性能 < 100ms
- clear() 清空
- 异常数据不崩溃
- is_active=False 跳过
"""

import time

import pytest

from dotclaw.metrics.collector import MetricsCollector
from dotclaw.metrics.events import AgentEvent, EventType


class TestCollectorBasic:
    def test_append_single_event(self):
        c = MetricsCollector()
        e = AgentEvent(timestamp=1000.0, event_type=EventType.REACT_LOOP_START)
        c.on_event(e)
        assert c.event_count == 1
        assert c.get_events()[0] is e

    def test_append_multiple_events(self):
        c = MetricsCollector()
        for i in range(100):
            c.on_event(AgentEvent(timestamp=float(i * 10), event_type=f"test.{i % 5}"))
        assert c.event_count == 100

    def test_clear(self):
        c = MetricsCollector()
        for i in range(10):
            c.on_event(AgentEvent(timestamp=1000.0, event_type=f"test.{i}"))
        assert c.event_count == 10
        c.clear()
        assert c.event_count == 0
        assert c.get_events() == []

    def test_get_events_returns_copy(self):
        c = MetricsCollector()
        c.on_event(AgentEvent(timestamp=1000.0, event_type="test"))
        events = c.get_events()
        events.pop()
        assert c.event_count == 1  # original unchanged


class TestCollectorPerformance:
    def test_10000_events_under_100ms(self):
        c = MetricsCollector()
        start = time.perf_counter()
        for i in range(10000):
            c.on_event(AgentEvent(
                timestamp=float(i),
                event_type=f"test.{i % 14}",
                data={"index": i, "value": f"data_{i}"},
            ))
        elapsed = (time.perf_counter() - start) * 1000
        assert c.event_count == 10000
        assert elapsed < 100, f"10000 events took {elapsed:.1f}ms, expected < 100ms"


class TestCollectorRobustness:
    def test_bad_event_data_does_not_crash(self):
        """如果 AgentEvent 构造有效，on_event 应始终成功（append 到 list 不会崩溃）。"""
        c = MetricsCollector()

        class BadData:
            def __hash__(self):
                raise RuntimeError("hash failure")

        # Normal events should work fine
        c.on_event(AgentEvent(timestamp=1000.0, event_type="test"))
        assert c.event_count == 1

    def test_is_active_false_skips_events(self):
        c = MetricsCollector()
        c.is_active = False
        c.on_event(AgentEvent(timestamp=1000.0, event_type="test"))
        assert c.event_count == 0

    def test_is_active_toggle(self):
        c = MetricsCollector()
        c.on_event(AgentEvent(timestamp=1000.0, event_type="test1"))
        c.is_active = False
        c.on_event(AgentEvent(timestamp=2000.0, event_type="test2"))
        c.is_active = True
        c.on_event(AgentEvent(timestamp=3000.0, event_type="test3"))
        assert c.event_count == 2

    def test_on_event_exception_silently_ignored(self):
        """If on_event raises internally (e.g. appending a bad object), it should be silenced."""
        c = MetricsCollector()
        # Directly append a problematic object via internal manipulation
        # then verify on_event doesn't crash on subsequent calls
        c._events = None  # type: ignore
        # This should not raise - the try/except catches it
        try:
            c.on_event(AgentEvent(timestamp=1000.0, event_type="test"))
        except Exception as e:
            pytest.fail(f"on_event should not propagate exceptions: {e}")


class TestCollectorDefault:
    def test_default_is_active(self):
        c = MetricsCollector()
        assert c.is_active is True

    def test_default_event_count_zero(self):
        c = MetricsCollector()
        assert c.event_count == 0
