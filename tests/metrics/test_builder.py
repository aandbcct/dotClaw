"""Phase 2 测试：SnapshotBuilder 从事件流构建快照。

覆盖：
- Golden Test（固定事件 → 固定快照）
- 空事件流 → 全零
- 幂等性
- 边界（P95、除零、单事件）
"""

import math

from dotclaw.metrics.builder import SnapshotBuilder, _p95, _safe_div
from dotclaw.metrics.events import AgentEvent, EventType
from dotclaw.metrics.snapshot import (
    AgentRunSnapshot,
    RunMeta,
)


def make_meta(run_id: str = "test_001") -> RunMeta:
    return RunMeta(
        run_id=run_id,
        timestamp="2026-06-07T00:00:00",
        git_commit="abc123",
        config_hash="xyz789",
        test_dataset="test_v1",
        test_dataset_size=3,
    )


class TestHelperFunctions:
    def test_p95_empty(self):
        assert _p95([]) == 0.0

    def test_p95_single(self):
        assert _p95([100.0]) == 100.0

    def test_p95_multiple(self):
        values = list(range(1, 101))  # 1..100
        # P95 of 1..100 → index = int(100*0.95) = 95 → values[95] = 96
        result = _p95([float(v) for v in values])
        assert result == 96.0

    def test_safe_div_normal(self):
        assert _safe_div(10.0, 5) == 2.0

    def test_safe_div_zero_denom(self):
        assert _safe_div(10.0, 0) == 0.0


class TestEmptyEvents:
    def test_build_empty(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        snapshot = builder.build()

        assert isinstance(snapshot, AgentRunSnapshot)
        assert snapshot.react.total_loops == 0
        assert snapshot.react.avg_loops_per_task == 0.0
        assert snapshot.tools.total_calls == 0
        assert snapshot.skills.total_triggers == 0
        assert snapshot.memory.total_retrievals == 0
        assert snapshot.general.total_input_tokens == 0

    def test_build_empty_all_zero(self):
        builder = SnapshotBuilder(make_meta(), task_count=5)
        snapshot = builder.build()

        # All numeric fields should be 0, dicts should be empty
        assert snapshot.react.task_completion_rate == 0.0
        assert snapshot.tools.overall_success_rate == 0.0
        assert snapshot.tools.calls_by_tool == {}
        assert snapshot.skills.triggers_by_skill == {}
        assert snapshot.memory.writes_by_type == {}
        assert snapshot.general.cost_by_model == {}


class TestGolden:
    """Golden test: fixed events → fixed expected snapshot values."""

    EVENTS = [
        # Task 1: success
        AgentEvent(1000.0, EventType.SESSION_START, {"session_id": "s1", "task_index": 0}),
        AgentEvent(1001.0, EventType.REACT_LOOP_START, {"loop_index": 0, "thought_tokens": 100}),
        AgentEvent(1002.0, EventType.TOOL_CALL_START, {"tool_name": "read"}),
        AgentEvent(1003.0, EventType.TOOL_CALL_END, {"tool_name": "read", "success": True, "duration_ms": 50.0}),
        AgentEvent(1004.0, EventType.LLM_REQUEST_START, {"model": "deepseek", "input_tokens": 500}),
        AgentEvent(1005.0, EventType.LLM_REQUEST_END, {"model": "deepseek", "output_tokens": 200, "duration_ms": 300.0, "ttft_ms": 100.0, "tps": 50.0}),
        AgentEvent(1006.0, EventType.MEMORY_RETRIEVAL, {"query": "test", "hit": True, "duration_ms": 80.0}),
        AgentEvent(1007.0, EventType.REACT_LOOP_END, {"loop_index": 0, "action": "read", "duration_ms": 400.0, "action_input": "file1"}),
        AgentEvent(1008.0, EventType.REACT_LOOP_START, {"loop_index": 1, "thought_tokens": 150}),
        AgentEvent(1009.0, EventType.TOOL_CALL_START, {"tool_name": "write"}),
        AgentEvent(1010.0, EventType.TOOL_CALL_END, {"tool_name": "write", "success": True, "duration_ms": 100.0}),
        AgentEvent(1011.0, EventType.TOOL_CALL_START, {"tool_name": "write"}),
        AgentEvent(1012.0, EventType.TOOL_CALL_END, {"tool_name": "write", "success": False, "duration_ms": 200.0, "error_type": "permission"}),
        AgentEvent(1013.0, EventType.LLM_REQUEST_START, {"model": "deepseek", "input_tokens": 300}),
        AgentEvent(1014.0, EventType.LLM_REQUEST_END, {"model": "deepseek", "output_tokens": 150, "duration_ms": 200.0, "ttft_ms": 80.0, "tps": 60.0}),
        AgentEvent(1015.0, EventType.REACT_LOOP_END, {"loop_index": 1, "action": "write", "duration_ms": 350.0, "action_input": "out1"}),
        AgentEvent(1016.0, EventType.SKILL_TRIGGER, {"skill_name": "xlsx", "trigger_source": "tool_result"}),
        AgentEvent(1017.0, EventType.SKILL_BODY_LOADED, {"skill_name": "xlsx", "duration_ms": 30.0, "cached": False, "token_count": 300}),
        AgentEvent(1018.0, EventType.SKILL_SCRIPT_EXEC, {"skill_name": "xlsx", "script_path": "xlsx/scripts/export.py", "success": True}),
        AgentEvent(1019.0, EventType.MEMORY_WRITE, {"memory_type": "daily_note", "success": True}),
        AgentEvent(1020.0, EventType.SESSION_END, {"session_id": "s1", "success": True, "total_duration_ms": 5000.0}),

        # Task 2: failure
        AgentEvent(2000.0, EventType.SESSION_START, {"session_id": "s2", "task_index": 1}),
        AgentEvent(2001.0, EventType.REACT_LOOP_START, {"loop_index": 0, "thought_tokens": 80}),
        AgentEvent(2002.0, EventType.TOOL_CALL_START, {"tool_name": "exec"}),
        AgentEvent(2003.0, EventType.TOOL_CALL_END, {"tool_name": "exec", "success": True, "duration_ms": 500.0}),
        AgentEvent(2004.0, EventType.LLM_REQUEST_START, {"model": "deepseek", "input_tokens": 400}),
        AgentEvent(2005.0, EventType.LLM_REQUEST_END, {"model": "deepseek", "output_tokens": 100, "duration_ms": 250.0, "ttft_ms": 90.0, "tps": 40.0}),
        AgentEvent(2006.0, EventType.MEMORY_RETRIEVAL, {"query": "test2", "hit": False, "duration_ms": 60.0}),
        AgentEvent(2007.0, EventType.REACT_LOOP_END, {"loop_index": 0, "action": "exec", "duration_ms": 800.0, "action_input": "cmd1"}),
        AgentEvent(2008.0, EventType.REACT_EMPTY_ACTION, {"loop_index": 1}),
        AgentEvent(2009.0, EventType.SESSION_END, {"session_id": "s2", "success": False, "total_duration_ms": 3000.0}),

        # Task 3: success with redundant action
        AgentEvent(3000.0, EventType.SESSION_START, {"session_id": "s3", "task_index": 2}),
        AgentEvent(3001.0, EventType.REACT_LOOP_START, {"loop_index": 0, "thought_tokens": 120}),
        AgentEvent(3002.0, EventType.TOOL_CALL_START, {"tool_name": "read"}),
        AgentEvent(3003.0, EventType.TOOL_CALL_END, {"tool_name": "read", "success": True, "duration_ms": 40.0}),
        AgentEvent(3004.0, EventType.LLM_REQUEST_START, {"model": "gpt-4o", "input_tokens": 600}),
        AgentEvent(3005.0, EventType.LLM_REQUEST_END, {"model": "gpt-4o", "output_tokens": 250, "duration_ms": 350.0, "ttft_ms": 120.0, "tps": 55.0}),
        AgentEvent(3006.0, EventType.REACT_LOOP_END, {"loop_index": 0, "action": "read", "duration_ms": 430.0, "action_input": "file2"}),
        AgentEvent(3007.0, EventType.REACT_LOOP_START, {"loop_index": 1, "thought_tokens": 50}),
        AgentEvent(3008.0, EventType.TOOL_CALL_START, {"tool_name": "read"}),
        AgentEvent(3009.0, EventType.TOOL_CALL_END, {"tool_name": "read", "success": True, "duration_ms": 35.0}),
        AgentEvent(3010.0, EventType.REACT_LOOP_END, {"loop_index": 1, "action": "read", "duration_ms": 100.0, "action_input": "file2"}),
        AgentEvent(3011.0, EventType.SESSION_END, {"session_id": "s3", "success": True, "total_duration_ms": 4500.0}),
    ]

    EXPECTED = {
        "react": {
            "total_loops": 5,
            "avg_loops_per_task": 1.67,
            "max_loops_single_task": 2,
            "task_completion_rate": 0.6667,
            "empty_action_rate": 0.2,
            "redundant_action_rate": 0.2,  # 1 redundant / 5 actions
            "avg_reasoning_tokens_per_loop": 100,
            "avg_loop_duration_ms": 416.0,
            "avg_llm_duration_ms": 275.0,
            "avg_tool_duration_ms": 154.2,
            "p95_loop_duration_ms": 800.0,
        },
        "tools": {
            "total_calls": 6,
            "calls_by_tool": {"read": 3, "write": 2, "exec": 1},
            "overall_success_rate": 0.8333,
            "success_rate_by_tool": {"read": 1.0, "write": 0.5, "exec": 1.0},
            "errors_by_tool": {"write": 1},
            "errors_by_type": {"permission": 1},
            "retry_rate": 0.1667,  # write called twice in same loop (task1 loop1)
            "avg_duration_by_tool": {"read": 41.7, "write": 150.0, "exec": 500.0},
            "p95_duration_by_tool": {"read": 50.0, "write": 200.0, "exec": 500.0},
        },
        "skills": {
            "total_triggers": 1,
            "triggers_by_skill": {"xlsx": 1},
            "trigger_rate": 0.3333,
            "avg_body_load_ms": 30.0,
            "body_cache_hit_rate": 0.0,
            "avg_scripts_per_trigger": 1.0,
            "script_success_rate": 1.0,
            "avg_skill_duration_ms": 30.0,
            "token_overhead_per_skill": 300.0,
        },
        "memory": {
            "total_retrievals": 2,
            "retrieval_rate": 0.6667,
            "hit_rate": 0.5,
            "avg_retrieval_ms": 70.0,
            "p95_retrieval_ms": 80.0,
            "total_writes": 1,
            "writes_by_type": {"daily_note": 1},
            "write_failures": 0,
        },
        "general": {
            "total_input_tokens": 1800,
            "total_output_tokens": 700,
            "avg_tokens_per_task": 833.3,
            "cost_usd": 0.0,
            "avg_ttft_ms": 97.5,
            "avg_tps": 51.2,
            "avg_e2e_latency_ms": 4166.7,
            "p95_e2e_latency_ms": 5000.0,
            "avg_context_length": 450,
        },
    }

    def test_golden_react(self):
        builder = SnapshotBuilder(make_meta(), task_count=3)
        for e in self.EVENTS:
            builder.process(e)
        snapshot = builder.build()

        exp = self.EXPECTED["react"]
        r = snapshot.react
        assert r.total_loops == exp["total_loops"]
        assert r.avg_loops_per_task == exp["avg_loops_per_task"]
        assert r.max_loops_single_task == exp["max_loops_single_task"]
        assert r.task_completion_rate == exp["task_completion_rate"]
        assert r.empty_action_rate == exp["empty_action_rate"]
        assert r.redundant_action_rate == exp["redundant_action_rate"]
        assert r.avg_reasoning_tokens_per_loop == exp["avg_reasoning_tokens_per_loop"]
        assert r.avg_loop_duration_ms == exp["avg_loop_duration_ms"]
        assert r.avg_llm_duration_ms == exp["avg_llm_duration_ms"]
        assert r.avg_tool_duration_ms == exp["avg_tool_duration_ms"]
        assert r.p95_loop_duration_ms == exp["p95_loop_duration_ms"]

    def test_golden_tools(self):
        builder = SnapshotBuilder(make_meta(), task_count=3)
        for e in self.EVENTS:
            builder.process(e)
        snapshot = builder.build()

        exp = self.EXPECTED["tools"]
        t = snapshot.tools
        assert t.total_calls == exp["total_calls"]
        assert t.calls_by_tool == exp["calls_by_tool"]
        assert t.overall_success_rate == exp["overall_success_rate"]
        assert t.success_rate_by_tool == exp["success_rate_by_tool"]
        assert t.errors_by_tool == exp["errors_by_tool"]
        assert t.errors_by_type == exp["errors_by_type"]
        assert t.retry_rate == exp["retry_rate"]
        assert t.avg_duration_by_tool == exp["avg_duration_by_tool"]
        assert t.p95_duration_by_tool == exp["p95_duration_by_tool"]

    def test_golden_skills(self):
        builder = SnapshotBuilder(make_meta(), task_count=3)
        for e in self.EVENTS:
            builder.process(e)
        snapshot = builder.build()

        exp = self.EXPECTED["skills"]
        s = snapshot.skills
        assert s.total_triggers == exp["total_triggers"]
        assert s.triggers_by_skill == exp["triggers_by_skill"]
        assert s.trigger_rate == exp["trigger_rate"]
        assert s.avg_body_load_ms == exp["avg_body_load_ms"]
        assert s.body_cache_hit_rate == exp["body_cache_hit_rate"]
        assert s.avg_scripts_per_trigger == exp["avg_scripts_per_trigger"]
        assert s.script_success_rate == exp["script_success_rate"]
        assert s.avg_skill_duration_ms == exp["avg_skill_duration_ms"]
        assert s.token_overhead_per_skill == exp["token_overhead_per_skill"]

    def test_golden_memory(self):
        builder = SnapshotBuilder(make_meta(), task_count=3)
        for e in self.EVENTS:
            builder.process(e)
        snapshot = builder.build()

        exp = self.EXPECTED["memory"]
        m = snapshot.memory
        assert m.total_retrievals == exp["total_retrievals"]
        assert m.retrieval_rate == exp["retrieval_rate"]
        assert m.hit_rate == exp["hit_rate"]
        assert m.avg_retrieval_ms == exp["avg_retrieval_ms"]
        assert m.p95_retrieval_ms == exp["p95_retrieval_ms"]
        assert m.total_writes == exp["total_writes"]
        assert m.writes_by_type == exp["writes_by_type"]
        assert m.write_failures == exp["write_failures"]

    def test_golden_general(self):
        builder = SnapshotBuilder(make_meta(), task_count=3)
        for e in self.EVENTS:
            builder.process(e)
        snapshot = builder.build()

        exp = self.EXPECTED["general"]
        g = snapshot.general
        assert g.total_input_tokens == exp["total_input_tokens"]
        assert g.total_output_tokens == exp["total_output_tokens"]
        assert g.avg_tokens_per_task == exp["avg_tokens_per_task"]
        assert g.cost_usd == exp["cost_usd"]
        assert g.avg_ttft_ms == exp["avg_ttft_ms"]
        assert g.avg_tps == exp["avg_tps"]
        assert g.avg_e2e_latency_ms == exp["avg_e2e_latency_ms"]
        assert g.p95_e2e_latency_ms == exp["p95_e2e_latency_ms"]
        assert g.avg_context_length == exp["avg_context_length"]


class TestIdempotency:
    def test_build_idempotent(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.SESSION_START, {"session_id": "s1"}))
        builder.process(AgentEvent(1001.0, EventType.REACT_LOOP_START, {"loop_index": 0}))
        builder.process(AgentEvent(1010.0, EventType.SESSION_END, {"session_id": "s1", "success": True}))

        s1 = builder.build()
        s2 = builder.build()
        # Compare react to verify idempotency
        assert s1.react == s2.react
        assert s1.tools == s2.tools

    def test_build_twice_same_result(self):
        builder = SnapshotBuilder(make_meta(), task_count=2)
        for e in TestGolden.EVENTS:
            builder.process(e)

        s1 = builder.build()
        s2 = builder.build()
        assert s1.react == s2.react
        assert s1.tools == s2.tools
        assert s1.skills == s2.skills
        assert s1.memory == s2.memory
        assert s1.general == s2.general


class TestBoundary:
    def test_single_event(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.REACT_LOOP_START, {"loop_index": 0}))
        snapshot = builder.build()
        assert snapshot.react.total_loops == 1
        assert snapshot.tools.total_calls == 0

    def test_no_session_end(self):
        """Loops without session.end still count but task_completion_rate = 0."""
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.SESSION_START, {}))
        builder.process(AgentEvent(1001.0, EventType.REACT_LOOP_START, {}))
        builder.process(AgentEvent(1002.0, EventType.REACT_LOOP_END, {"duration_ms": 100.0}))
        snapshot = builder.build()
        assert snapshot.react.total_loops == 1
        assert snapshot.react.task_completion_rate == 0.0  # no success recorded

    def test_unknown_event_type(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, "unknown.event.type", {}))
        snapshot = builder.build()
        # Unknown events should not crash, snapshot should still be valid
        assert isinstance(snapshot, AgentRunSnapshot)
        assert snapshot.react.total_loops == 0

    def test_duration_zero_handling(self):
        """Events with duration_ms=0 should not contribute to averages."""
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.REACT_LOOP_END, {"duration_ms": 0.0}))
        builder.process(AgentEvent(1001.0, EventType.MEMORY_RETRIEVAL, {"duration_ms": 0.0}))
        snapshot = builder.build()
        assert snapshot.react.avg_loop_duration_ms == 0.0
        assert snapshot.memory.avg_retrieval_ms == 0.0

    def test_task_count_floor(self):
        """task_count=0 should not divide by zero."""
        builder = SnapshotBuilder(make_meta(), task_count=0)
        builder.process(AgentEvent(1000.0, EventType.SESSION_START, {}))
        builder.process(AgentEvent(1001.0, EventType.REACT_LOOP_START, {}))
        builder.process(AgentEvent(1010.0, EventType.SESSION_END, {"success": True}))
        snapshot = builder.build()
        # Should default to using session count as denominator
        assert snapshot.react.task_completion_rate == 1.0

    def test_cost_by_model_tracks_all_models(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.LLM_REQUEST_START, {"model": "deepseek", "input_tokens": 100}))
        builder.process(AgentEvent(1001.0, EventType.LLM_REQUEST_END, {"model": "deepseek", "output_tokens": 50}))
        builder.process(AgentEvent(2000.0, EventType.LLM_REQUEST_START, {"model": "gpt-4o", "input_tokens": 200}))
        builder.process(AgentEvent(2001.0, EventType.LLM_REQUEST_END, {"model": "gpt-4o", "output_tokens": 100}))
        snapshot = builder.build()
        assert set(snapshot.general.cost_by_model.keys()) == {"deepseek", "gpt-4o"}

    def test_skill_cached_hit(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.SKILL_BODY_LOADED,
            {"skill_name": "test", "duration_ms": 10.0, "cached": True, "token_count": 100}))
        snapshot = builder.build()
        assert snapshot.skills.body_cache_hit_rate == 1.0

    def test_memory_write_failure(self):
        builder = SnapshotBuilder(make_meta(), task_count=1)
        builder.process(AgentEvent(1000.0, EventType.MEMORY_WRITE,
            {"memory_type": "daily_note", "success": False}))
        snapshot = builder.build()
        assert snapshot.memory.write_failures == 1
        assert snapshot.memory.total_writes == 1
