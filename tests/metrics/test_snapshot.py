"""Phase 0 类型级测试：验证所有 dataclass 的 frozen 约束和字段数。"""

import pytest

from dotclaw.metrics.events import AgentEvent, EventType
from dotclaw.metrics.snapshot import (
    AgentGeneralMetrics,
    AgentRunSnapshot,
    MemoryMetrics,
    ReactLoopMetrics,
    RunMeta,
    SkillMetrics,
    ToolCallMetrics,
)


class TestAgentEvent:
    def test_construction(self):
        e = AgentEvent(
            timestamp=1000.0,
            event_type=EventType.REACT_LOOP_START,
            data={"loop_index": 0},
        )
        assert e.timestamp == 1000.0
        assert e.event_type == EventType.REACT_LOOP_START
        assert e.data == {"loop_index": 0}

    def test_frozen(self):
        e = AgentEvent(timestamp=1000.0, event_type="test")
        with pytest.raises(Exception):
            e.timestamp = 2000.0  # type: ignore

    def test_default_data(self):
        e = AgentEvent(timestamp=1000.0, event_type="test")
        assert e.data == {}


class TestEventType:
    def test_14_event_types(self):
        constants = [
            EventType.REACT_LOOP_START,
            EventType.REACT_LOOP_END,
            EventType.REACT_EMPTY_ACTION,
            EventType.TOOL_CALL_START,
            EventType.TOOL_CALL_END,
            EventType.SKILL_TRIGGER,
            EventType.SKILL_BODY_LOADED,
            EventType.SKILL_SCRIPT_EXEC,
            EventType.MEMORY_RETRIEVAL,
            EventType.MEMORY_WRITE,
            EventType.LLM_REQUEST_START,
            EventType.LLM_REQUEST_END,
            EventType.SESSION_START,
            EventType.SESSION_END,
        ]
        assert len(constants) == 14
        assert len(set(constants)) == 14  # all unique


class TestRunMeta:
    def test_construction(self):
        m = RunMeta(
            run_id="r1",
            timestamp="2026-06-07T00:00:00",
            git_commit="abc123",
            config_hash="xyz789",
            test_dataset="bench_v1",
            test_dataset_size=50,
        )
        assert m.run_id == "r1"
        assert m.test_dataset_size == 50

    def test_frozen(self):
        m = RunMeta(run_id="r1", timestamp="", git_commit="", config_hash="", test_dataset="", test_dataset_size=0)
        with pytest.raises(Exception):
            m.run_id = "r2"  # type: ignore


class TestReactLoopMetrics:
    EXPECTED_FIELDS = 11

    def test_field_count(self):
        import dataclasses
        fields = dataclasses.fields(ReactLoopMetrics)
        assert len(fields) == self.EXPECTED_FIELDS, f"Expected {self.EXPECTED_FIELDS}, got {len(fields)}"

    def test_defaults(self):
        m = ReactLoopMetrics()
        assert m.total_loops == 0
        assert m.avg_loops_per_task == 0.0
        assert m.max_loops_single_task == 0
        assert m.task_completion_rate == 0.0
        assert m.empty_action_rate == 0.0
        assert m.redundant_action_rate == 0.0
        assert m.avg_reasoning_tokens_per_loop == 0
        assert m.avg_loop_duration_ms == 0.0
        assert m.avg_llm_duration_ms == 0.0
        assert m.avg_tool_duration_ms == 0.0
        assert m.p95_loop_duration_ms == 0.0

    def test_frozen(self):
        m = ReactLoopMetrics()
        with pytest.raises(Exception):
            m.total_loops = 1  # type: ignore


class TestToolCallMetrics:
    EXPECTED_FIELDS = 9

    def test_field_count(self):
        import dataclasses
        fields = dataclasses.fields(ToolCallMetrics)
        assert len(fields) == self.EXPECTED_FIELDS, f"Expected {self.EXPECTED_FIELDS}, got {len(fields)}"

    def test_defaults(self):
        m = ToolCallMetrics()
        assert m.total_calls == 0
        assert m.calls_by_tool == {}
        assert m.overall_success_rate == 0.0
        assert m.success_rate_by_tool == {}
        assert m.errors_by_tool == {}
        assert m.errors_by_type == {}
        assert m.retry_rate == 0.0
        assert m.avg_duration_by_tool == {}
        assert m.p95_duration_by_tool == {}

    def test_frozen(self):
        m = ToolCallMetrics()
        with pytest.raises(Exception):
            m.total_calls = 1  # type: ignore


class TestSkillMetrics:
    EXPECTED_FIELDS = 9

    def test_field_count(self):
        import dataclasses
        fields = dataclasses.fields(SkillMetrics)
        assert len(fields) == self.EXPECTED_FIELDS, f"Expected {self.EXPECTED_FIELDS}, got {len(fields)}"

    def test_defaults(self):
        m = SkillMetrics()
        assert m.total_triggers == 0
        assert m.triggers_by_skill == {}
        assert m.trigger_rate == 0.0
        assert m.avg_body_load_ms == 0.0
        assert m.body_cache_hit_rate == 0.0
        assert m.avg_scripts_per_trigger == 0.0
        assert m.script_success_rate == 0.0
        assert m.avg_skill_duration_ms == 0.0
        assert m.token_overhead_per_skill == 0.0

    def test_frozen(self):
        m = SkillMetrics()
        with pytest.raises(Exception):
            m.total_triggers = 1  # type: ignore


class TestMemoryMetrics:
    EXPECTED_FIELDS = 12

    def test_field_count(self):
        import dataclasses
        fields = dataclasses.fields(MemoryMetrics)
        assert len(fields) == self.EXPECTED_FIELDS, f"Expected {self.EXPECTED_FIELDS}, got {len(fields)}"

    def test_defaults(self):
        m = MemoryMetrics()
        assert m.total_retrievals == 0
        assert m.retrieval_rate == 0.0
        assert m.hit_rate == 0.0
        assert m.avg_retrieval_ms == 0.0
        assert m.p95_retrieval_ms == 0.0
        assert m.index_size == 0
        assert m.index_size_mb == 0.0
        assert m.total_writes == 0
        assert m.writes_by_type == {}
        assert m.write_failures == 0
        assert m.avg_memory_tokens_per_request == 0.0
        assert m.memory_token_ratio == 0.0

    def test_frozen(self):
        m = MemoryMetrics()
        with pytest.raises(Exception):
            m.total_retrievals = 1  # type: ignore


class TestAgentGeneralMetrics:
    EXPECTED_FIELDS = 11

    def test_field_count(self):
        import dataclasses
        fields = dataclasses.fields(AgentGeneralMetrics)
        assert len(fields) == self.EXPECTED_FIELDS, f"Expected {self.EXPECTED_FIELDS}, got {len(fields)}"

    def test_defaults(self):
        m = AgentGeneralMetrics()
        assert m.total_input_tokens == 0
        assert m.total_output_tokens == 0
        assert m.avg_tokens_per_task == 0.0
        assert m.cost_usd == 0.0
        assert m.cost_by_model == {}
        assert m.avg_ttft_ms == 0.0
        assert m.avg_tps == 0.0
        assert m.avg_e2e_latency_ms == 0.0
        assert m.p95_e2e_latency_ms == 0.0
        assert m.avg_context_length == 0
        assert m.context_overflow_count == 0

    def test_frozen(self):
        m = AgentGeneralMetrics()
        with pytest.raises(Exception):
            m.total_input_tokens = 1  # type: ignore


class TestAgentRunSnapshot:
    def test_construction_defaults(self):
        s = AgentRunSnapshot()
        assert s.run_id == ""
        assert isinstance(s.react, ReactLoopMetrics)
        assert isinstance(s.tools, ToolCallMetrics)
        assert isinstance(s.skills, SkillMetrics)
        assert isinstance(s.memory, MemoryMetrics)
        assert isinstance(s.general, AgentGeneralMetrics)

    def test_construction_with_metrics(self):
        react = ReactLoopMetrics(total_loops=10)
        snapshot = AgentRunSnapshot(run_id="test", react=react)
        assert snapshot.run_id == "test"
        assert snapshot.react.total_loops == 10

    def test_frozen(self):
        s = AgentRunSnapshot()
        with pytest.raises(Exception):
            s.run_id = "new_id"  # type: ignore

    def test_meta_fields(self):
        s = AgentRunSnapshot(
            run_id="r1",
            timestamp="2026-06-07T00:00:00",
            git_commit="abc123",
            config_hash="xyz",
            test_dataset="bench_v1",
            test_dataset_size=50,
        )
        assert s.run_id == "r1"
        assert s.timestamp == "2026-06-07T00:00:00"
        assert s.git_commit == "abc123"
        assert s.config_hash == "xyz"
        assert s.test_dataset == "bench_v1"
        assert s.test_dataset_size == 50
