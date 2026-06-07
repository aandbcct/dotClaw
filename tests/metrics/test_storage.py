"""Phase 4 测试：快照 JSON 序列化、文件读写、diff 对比。"""

import json
import tempfile
from pathlib import Path

import pytest

from dotclaw.metrics.builder import SnapshotBuilder
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
from dotclaw.metrics.storage import (
    _build_config_hash,
    _get_git_commit,
    _is_improvement,
    build_run_meta,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_from_json,
    snapshot_to_json,
)


def make_snapshot(run_id: str = "test_roundtrip") -> AgentRunSnapshot:
    react = ReactLoopMetrics(
        total_loops=10,
        avg_loops_per_task=2.5,
        max_loops_single_task=5,
        task_completion_rate=0.9,
        empty_action_rate=0.05,
        redundant_action_rate=0.03,
        avg_reasoning_tokens_per_loop=200,
        avg_loop_duration_ms=1500.0,
        avg_llm_duration_ms=1200.0,
        avg_tool_duration_ms=300.0,
        p95_loop_duration_ms=5000.0,
    )
    tools = ToolCallMetrics(
        total_calls=20,
        calls_by_tool={"read": 10, "write": 7, "exec": 3},
        overall_success_rate=0.85,
        success_rate_by_tool={"read": 0.9, "write": 0.857, "exec": 0.667},
        errors_by_tool={"exec": 1, "read": 1},
        errors_by_type={"timeout": 2},
        retry_rate=0.1,
        avg_duration_by_tool={"read": 100.0, "write": 200.0, "exec": 500.0},
        p95_duration_by_tool={"read": 200.0, "write": 300.0, "exec": 800.0},
    )
    skills = SkillMetrics(
        total_triggers=5,
        triggers_by_skill={"xlsx": 3, "pdf": 2},
        trigger_rate=1.0,
        avg_body_load_ms=25.0,
        body_cache_hit_rate=0.8,
        avg_scripts_per_trigger=1.2,
        script_success_rate=0.95,
        avg_skill_duration_ms=50.0,
        token_overhead_per_skill=400.0,
    )
    memory = MemoryMetrics(
        total_retrievals=12,
        retrieval_rate=0.8,
        hit_rate=0.75,
        avg_retrieval_ms=80.0,
        p95_retrieval_ms=150.0,
        index_size=0,
        index_size_mb=0.0,
        total_writes=8,
        writes_by_type={"daily_note": 6, "long_term": 2},
        write_failures=0,
        avg_memory_tokens_per_request=300.0,
        memory_token_ratio=0.04,
    )
    general = AgentGeneralMetrics(
        total_input_tokens=50000,
        total_output_tokens=30000,
        avg_tokens_per_task=2000.0,
        cost_usd=0.0,
        cost_by_model={"deepseek": 0.0},
        avg_ttft_ms=350.0,
        avg_tps=80.0,
        avg_e2e_latency_ms=8000.0,
        p95_e2e_latency_ms=12000.0,
        avg_context_length=10000,
        context_overflow_count=0,
    )
    return AgentRunSnapshot(
        run_id=run_id,
        timestamp="2026-06-07T00:00:00+00:00",
        git_commit="abc123",
        config_hash="xyz789",
        test_dataset="bench_v1",
        test_dataset_size=50,
        react=react,
        tools=tools,
        skills=skills,
        memory=memory,
        general=general,
    )


class TestJsonRoundTrip:
    def test_to_json_and_back(self):
        original = make_snapshot()
        json_str = snapshot_to_json(original)
        restored = snapshot_from_json(json_str)

        assert restored.run_id == original.run_id
        assert restored.timestamp == original.timestamp
        assert restored.git_commit == original.git_commit
        assert restored.config_hash == original.config_hash
        assert restored.test_dataset == original.test_dataset
        assert restored.test_dataset_size == original.test_dataset_size

        # React
        assert restored.react == original.react
        # Tools
        assert restored.tools == original.tools
        # Skills
        assert restored.skills == original.skills
        # Memory
        assert restored.memory == original.memory
        # General
        assert restored.general == original.general

    def test_json_is_valid_and_indented(self):
        snapshot = make_snapshot()
        json_str = snapshot_to_json(snapshot)
        parsed = json.loads(json_str)
        assert parsed["run_id"] == "test_roundtrip"
        # verify indentation
        lines = json_str.split("\n")
        assert len(lines) > 1

    def test_preserves_all_fields(self):
        original = make_snapshot()
        json_str = snapshot_to_json(original)
        restored = snapshot_from_json(json_str)
        # Compare all metrics objects
        assert restored.react == original.react
        assert restored.tools == original.tools
        assert restored.skills == original.skills
        assert restored.memory == original.memory
        assert restored.general == original.general

    def test_empty_snapshot_round_trip(self):
        snapshot = AgentRunSnapshot(run_id="empty")
        json_str = snapshot_to_json(snapshot)
        restored = snapshot_from_json(json_str)
        assert restored.run_id == "empty"
        assert restored.react.total_loops == 0

    def test_missing_fields_get_defaults(self):
        json_str = '{"run_id": "partial"}'
        snapshot = snapshot_from_json(json_str)
        assert snapshot.run_id == "partial"
        assert snapshot.react.total_loops == 0
        assert snapshot.tools.calls_by_tool == {}


class TestFileIO:
    def test_save_and_load(self):
        snapshot = make_snapshot("file_test")
        with tempfile.TemporaryDirectory() as tmpdir:
            save_snapshot(snapshot, tmpdir)
            loaded = load_snapshot(Path(tmpdir) / "file_test.json")
            assert loaded == snapshot

    def test_save_creates_directory(self):
        snapshot = make_snapshot("dir_test")
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "nested" / "sub"
            save_snapshot(snapshot, nested)
            loaded = load_snapshot(nested / "dir_test.json")
            assert loaded.run_id == "dir_test"


class TestDiffSnapshots:
    def make_baseline(self) -> AgentRunSnapshot:
        return make_snapshot("baseline")

    def make_better_candidate(self) -> AgentRunSnapshot:
        s = make_snapshot("candidate")
        # Improve: higher completion, lower latency
        return AgentRunSnapshot(
            run_id="candidate",
            timestamp=s.timestamp,
            git_commit=s.git_commit,
            config_hash=s.config_hash,
            test_dataset=s.test_dataset,
            test_dataset_size=s.test_dataset_size,
            react=ReactLoopMetrics(
                total_loops=8,
                avg_loops_per_task=2.0,
                max_loops_single_task=4,
                task_completion_rate=0.95,
                empty_action_rate=0.02,
                redundant_action_rate=0.01,
                avg_reasoning_tokens_per_loop=180,
                avg_loop_duration_ms=1000.0,
                avg_llm_duration_ms=800.0,
                avg_tool_duration_ms=200.0,
                p95_loop_duration_ms=3000.0,
            ),
            tools=ToolCallMetrics(
                total_calls=15,
                calls_by_tool={"read": 8, "write": 5, "exec": 2},
                overall_success_rate=0.93,
                success_rate_by_tool={"read": 0.95, "write": 0.9, "exec": 1.0},
                errors_by_tool={},
                errors_by_type={},
                retry_rate=0.05,
                avg_duration_by_tool={"read": 80.0, "write": 150.0, "exec": 400.0},
                p95_duration_by_tool={"read": 150.0, "write": 200.0, "exec": 600.0},
            ),
            skills=s.skills,
            memory=s.memory,
            general=AgentGeneralMetrics(
                total_input_tokens=40000,
                total_output_tokens=25000,
                avg_tokens_per_task=1500.0,
                cost_usd=0.0,
                cost_by_model={"deepseek": 0.0},
                avg_ttft_ms=300.0,
                avg_tps=90.0,
                avg_e2e_latency_ms=6000.0,
                p95_e2e_latency_ms=9000.0,
                avg_context_length=8000,
                context_overflow_count=1,
            ),
        )

    def make_worse_candidate(self) -> AgentRunSnapshot:
        s = make_snapshot("candidate_worse")
        return AgentRunSnapshot(
            run_id="candidate_worse",
            timestamp=s.timestamp,
            git_commit=s.git_commit,
            config_hash=s.config_hash,
            test_dataset=s.test_dataset,
            test_dataset_size=s.test_dataset_size,
            react=ReactLoopMetrics(
                total_loops=15,
                avg_loops_per_task=3.5,
                max_loops_single_task=8,
                task_completion_rate=0.7,
                empty_action_rate=0.1,
                redundant_action_rate=0.08,
                avg_reasoning_tokens_per_loop=300,
                avg_loop_duration_ms=3000.0,
                avg_llm_duration_ms=2500.0,
                avg_tool_duration_ms=500.0,
                p95_loop_duration_ms=10000.0,
            ),
            tools=ToolCallMetrics(
                total_calls=30,
                overall_success_rate=0.6,
                success_rate_by_tool={},
                calls_by_tool={},
                errors_by_tool={},
                errors_by_type={},
                retry_rate=0.25,
                avg_duration_by_tool={},
                p95_duration_by_tool={},
            ),
            skills=s.skills,
            memory=s.memory,
            general=AgentGeneralMetrics(
                total_input_tokens=70000,
                total_output_tokens=40000,
                avg_tokens_per_task=3000.0,
                cost_usd=0.0,
                cost_by_model={"deepseek": 0.0},
                avg_ttft_ms=500.0,
                avg_tps=50.0,
                avg_e2e_latency_ms=12000.0,
                p95_e2e_latency_ms=20000.0,
                avg_context_length=15000,
                context_overflow_count=2,
            ),
        )

    def test_identical_snapshots_no_diff(self):
        baseline = self.make_baseline()
        lines = diff_snapshots(baseline, baseline)
        assert lines == []

    def test_improvement_marked_correctly(self):
        baseline = self.make_baseline()
        candidate = self.make_better_candidate()
        lines = diff_snapshots(baseline, candidate)

        assert len(lines) > 0
        for line in lines:
            # Lower latency, lower token count → should be improvement (✅)
            if "duration_ms" in line or "tokens" in line or "loops" in line:
                assert "✅" in line, f"Expected improvement: {line}"
            # Higher success rate → should be improvement (✅)
            if "completion" in line or "overall_success" in line:
                assert "✅" in line, f"Expected improvement: {line}"

    def test_regression_marked_correctly(self):
        baseline = self.make_baseline()
        candidate = self.make_worse_candidate()
        lines = diff_snapshots(baseline, candidate)

        assert len(lines) > 0
        for line in lines:
            if "duration_ms" in line or "tokens" in line or "loops" in line:
                # These went up → should be ❌
                assert "❌" in line, f"Expected regression: {line}"
            if "completion" in line or "overall_success" in line:
                # These went down → should be ❌
                assert "❌" in line, f"Expected regression: {line}"

    def test_small_changes_filtered(self):
        baseline = self.make_baseline()
        # Make a candidate with very small changes (< 2%)
        candidate = AgentRunSnapshot(
            run_id="small_delta",
            timestamp=baseline.timestamp,
            git_commit=baseline.git_commit,
            config_hash=baseline.config_hash,
            test_dataset=baseline.test_dataset,
            test_dataset_size=baseline.test_dataset_size,
            react=ReactLoopMetrics(
                total_loops=10,  # unchanged
                avg_loops_per_task=2.51,  # +0.4% < 2%
                max_loops_single_task=5,
                task_completion_rate=0.901,  # +0.11% < 2%
                empty_action_rate=0.05,
                redundant_action_rate=0.03,
                avg_reasoning_tokens_per_loop=200,
                avg_loop_duration_ms=1500.0,
                avg_llm_duration_ms=1200.0,
                avg_tool_duration_ms=300.0,
                p95_loop_duration_ms=5000.0,
            ),
            tools=baseline.tools,
            skills=baseline.skills,
            memory=baseline.memory,
            general=baseline.general,
        )
        lines = diff_snapshots(baseline, candidate)
        # Small changes should be filtered out
        assert lines == []

    def test_diff_format(self):
        baseline = self.make_baseline()
        candidate = self.make_better_candidate()
        lines = diff_snapshots(baseline, candidate)

        for line in lines:
            # " -> " for percentage changes, "N/A →" for new-from-zero
            has_change = " -> " in line and "%" in line
            has_new = "N/A →" in line
            assert has_change or has_new, f"Unexpected format: {line}"
            if has_change:
                assert "✅" in line or "❌" in line, f"Missing marker: {line}"

    def test_baseline_zero_shows_new(self):
        """Fields where baseline is 0 should output 'N/A → val (new)' instead of being skipped."""
        baseline = AgentRunSnapshot(
            run_id="zero",
            react=ReactLoopMetrics(total_loops=0),
            tools=ToolCallMetrics(overall_success_rate=0.0),
            skills=SkillMetrics(),
            memory=MemoryMetrics(),
            general=AgentGeneralMetrics(),
        )
        candidate = AgentRunSnapshot(
            run_id="non_zero",
            react=ReactLoopMetrics(total_loops=5),
            tools=ToolCallMetrics(overall_success_rate=0.5),
            skills=SkillMetrics(),
            memory=MemoryMetrics(),
            general=AgentGeneralMetrics(),
        )
        lines = diff_snapshots(baseline, candidate)
        # Both fields went from 0 to non-zero, so they should appear as "new"
        assert any("N/A →" in l for l in lines)
        assert len(lines) >= 1


class TestIsImprovement:
    def test_success_rate_up_is_improvement(self):
        assert _is_improvement("overall_success_rate", 10.0) is True

    def test_success_rate_down_is_regression(self):
        assert _is_improvement("overall_success_rate", -5.0) is False

    def test_latency_up_is_regression(self):
        assert _is_improvement("avg_loop_duration_ms", 60.0) is False

    def test_latency_down_is_improvement(self):
        assert _is_improvement("avg_loop_duration_ms", -30.0) is True

    def test_tokens_up_is_regression(self):
        assert _is_improvement("total_input_tokens", 15.0) is False

    def test_cost_up_is_regression(self):
        assert _is_improvement("cost_usd", 20.0) is False

    def test_failures_up_is_regression(self):
        assert _is_improvement("write_failures", 100.0) is False

    def test_small_change_not_improvement(self):
        assert _is_improvement("overall_success_rate", 1.5) is False
        assert _is_improvement("avg_loop_duration_ms", -1.5) is False

    def test_empty_action_rate_up_is_regression(self):
        """empty_action_rate higher = worse, should be regression despite 'rate' keyword."""
        assert _is_improvement("react.empty_action_rate", 100.0) is False

    def test_empty_action_rate_down_is_improvement(self):
        assert _is_improvement("react.empty_action_rate", -50.0) is True

    def test_redundant_action_rate_up_is_regression(self):
        assert _is_improvement("react.redundant_action_rate", 200.0) is False

    def test_retry_rate_up_is_regression(self):
        assert _is_improvement("tools.retry_rate", 150.0) is False


class TestRunMetaBuilder:
    def test_git_commit_returns_string(self):
        commit = _get_git_commit()
        assert isinstance(commit, str)
        assert len(commit) > 0

    def test_config_hash_from_files(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f1:
            f1.write(b"model: deepseek\n")
            f1.flush()
            with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f2:
                f2.write(b"router: default\n")
                f2.flush()
                h = _build_config_hash(f1.name, f2.name)
                assert isinstance(h, str)
                assert len(h) == 16

    def test_config_hash_missing_file(self):
        h = _build_config_hash("nonexistent.yaml", "also_nonexistent.yaml")
        assert h == "unknown"

    def test_build_run_meta(self):
        meta = build_run_meta(
            run_id="test_001",
            test_dataset="bench_v1",
            test_dataset_size=50,
        )
        assert meta.run_id == "test_001"
        assert meta.test_dataset == "bench_v1"
        assert meta.test_dataset_size == 50
        assert len(meta.timestamp) > 0
        # git_commit and config_hash should be non-empty strings
        assert isinstance(meta.git_commit, str)
        assert isinstance(meta.config_hash, str)
        assert len(meta.git_commit) > 0
